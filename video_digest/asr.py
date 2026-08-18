from __future__ import annotations

import importlib
import importlib.util
import json
import math
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol, cast

from .diagnostics import sanitize_external_diagnostic
from .domain import DigestFailure, EvidenceBundle, TranscriptSegment, VideoMetadata, VideoRequest
from .video_urls import video_reference


@dataclass(frozen=True)
class AsrModelProbe:
    model_name: str
    available_locally: bool
    estimated_download_bytes: int
    device: str = "cpu"
    compute_type: str = "int8"


@dataclass(frozen=True)
class AsrModelNotice:
    model_name: str
    estimated_download_bytes: int
    device: str
    compute_type: str


@dataclass(frozen=True)
class AsrTranscript:
    segments: tuple[TranscriptSegment, ...]
    language: str | None


class AsrBackendFailure(RuntimeError):
    def __init__(
        self,
        *,
        stage: str,
        code: str,
        message: str,
        retryable: bool,
        exit_status: int | None = None,
        stderr_summary: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.message = message
        self.retryable = retryable
        self.exit_status = exit_status
        self.stderr_summary = stderr_summary


class AudioBackend(Protocol):
    def inspect(self, request: VideoRequest) -> VideoMetadata: ...

    def download(self, request: VideoRequest, directory: Path) -> Path: ...


class LocalTranscriber(Protocol):
    def probe(self) -> AsrModelProbe: ...

    def prepare(self, *, allow_download: bool) -> None: ...

    def transcribe(self, audio_path: Path) -> AsrTranscript: ...


class AsrCommandRunner(Protocol):
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]: ...


class DefaultAsrCommandRunner:
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            text=True,
            timeout=timeout_seconds,
        )


class YtDlpAudioBackend:
    def __init__(
        self,
        *,
        timeout_seconds: float = 600.0,
        runner: AsrCommandRunner | None = None,
        cookies_from_browser: str | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._runner = runner or DefaultAsrCommandRunner()
        self._cookies_from_browser = cookies_from_browser

    def inspect(self, request: VideoRequest) -> VideoMetadata:
        reference = video_reference(request.url)
        completed = self._run(
            [
                *self._base_command(skip_download=True),
                "--dump-single-json",
                reference.canonical_url,
            ],
            stage="audio_metadata",
        )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AsrBackendFailure(
                stage="audio_metadata",
                code="audio_metadata_parse_failed",
                message="yt-dlp returned unexpected metadata before local ASR.",
                retryable=True,
                exit_status=completed.returncode,
                stderr_summary=sanitize_external_diagnostic(completed.stderr),
            ) from error
        if not isinstance(value, dict):
            raise AsrBackendFailure(
                stage="audio_metadata",
                code="audio_metadata_parse_failed",
                message="yt-dlp returned unexpected metadata before local ASR.",
                retryable=True,
                exit_status=completed.returncode,
                stderr_summary=sanitize_external_diagnostic(completed.stderr),
            )
        return VideoMetadata(
            platform=reference.platform,
            video_id=reference.video_id,
            title=_optional_text(value.get("title")) or reference.video_id,
            channel=_optional_text(value.get("channel"))
            or _optional_text(value.get("uploader")),
            duration_seconds=_optional_duration(value.get("duration")),
            canonical_url=reference.canonical_url,
        )

    def download(self, request: VideoRequest, directory: Path) -> Path:
        reference = video_reference(request.url)
        output_template = str(directory / "%(id)s.%(ext)s")
        self._run(
            [
                *self._base_command(skip_download=False),
                "--format",
                "bestaudio",
                "--output",
                output_template,
                reference.canonical_url,
            ],
            stage="audio",
        )
        files = tuple(path for path in directory.rglob("*") if path.is_file())
        audio_files = tuple(
            path for path in files if path.suffix.casefold() in _AUDIO_SUFFIXES
        )
        if not audio_files:
            raise AsrBackendFailure(
                stage="audio",
                code="audio_download_missing",
                message="The audio-only command completed without a usable audio artifact.",
                retryable=True,
            )
        if len(audio_files) != 1 or len(files) != 1:
            raise AsrBackendFailure(
                stage="audio",
                code="audio_download_ambiguous",
                message="The audio-only command produced an unexpected artifact set.",
                retryable=False,
            )
        return audio_files[0]

    def _base_command(self, *, skip_download: bool) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--ignore-no-formats-error",
            "--no-colors",
            "--quiet",
            "--no-warnings",
        ]
        if skip_download:
            command.append("--skip-download")
        if self._cookies_from_browser is not None:
            command.extend(("--cookies-from-browser", self._cookies_from_browser))
        return command

    def _run(
        self,
        command: list[str],
        *,
        stage: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._runner.run(command, self._timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise AsrBackendFailure(
                stage=stage,
                code="timeout",
                message=f"The local ASR {stage} operation timed out.",
                retryable=True,
            ) from error
        if completed.returncode != 0:
            raise _classify_audio_failure(
                stage,
                f"{completed.stdout}\n{completed.stderr}",
                completed.returncode,
            )
        return completed


class FasterWhisperTranscriber:
    def __init__(
        self,
        *,
        model_name: str = "small",
        model_directory: Path,
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 0,
    ) -> None:
        self.model_name = model_name
        self.model_directory = model_directory
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self._model: _WhisperModel | None = None

    def probe(self) -> AsrModelProbe:
        try:
            module_spec = importlib.util.find_spec("faster_whisper")
        except (ImportError, ValueError) as error:
            raise _dependency_failure() from error
        if module_spec is None:
            raise _dependency_failure()
        try:
            importlib.import_module("faster_whisper")
            ctranslate2 = importlib.import_module("ctranslate2")
            importlib.import_module("av")
        except (ImportError, OSError, RuntimeError) as error:
            raise AsrBackendFailure(
                stage="asr_dependency",
                code="asr_dependency_unavailable",
                message=(
                    "Local ASR packages are installed, but a required native runtime could "
                    "not be loaded. Resynchronize the optional ASR environment and retry."
                ),
                retryable=False,
                stderr_summary=sanitize_external_diagnostic(
                    f"{type(error).__name__}: {error}"
                ),
            ) from error
        _validate_execution_configuration(
            ctranslate2,
            device=self.device,
            compute_type=self.compute_type,
        )
        return AsrModelProbe(
            model_name=self.model_name,
            available_locally=_model_files_are_complete(self.model_directory),
            estimated_download_bytes=_estimated_model_bytes(self.model_name),
            device=self.device,
            compute_type=self.compute_type,
        )

    def prepare(self, *, allow_download: bool) -> None:
        probe = self.probe()
        if not probe.available_locally and not allow_download:
            raise AsrBackendFailure(
                stage="asr_model",
                code="asr_model_required",
                message=_model_required_message(probe),
                retryable=False,
            )
        try:
            if not probe.available_locally:
                utils_module = importlib.import_module("faster_whisper.utils")
                download_model = utils_module.download_model
                download_model(
                    self.model_name,
                    output_dir=str(self.model_directory),
                    local_files_only=False,
                )
            faster_whisper = importlib.import_module("faster_whisper")
            model_factory = faster_whisper.WhisperModel
            model = model_factory(
                str(self.model_directory),
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                local_files_only=True,
            )
        except Exception as error:
            raise AsrBackendFailure(
                stage="asr_model",
                code=(
                    "asr_model_download_failed"
                    if not probe.available_locally
                    else "asr_model_load_failed"
                ),
                message=(
                    "The faster-whisper model could not be downloaded locally."
                    if not probe.available_locally
                    else "The local faster-whisper model could not be loaded."
                ),
                retryable=True,
                stderr_summary=sanitize_external_diagnostic(
                    f"{type(error).__name__}: {error}"
                ),
            ) from error
        self._model = cast(_WhisperModel, model)

    def transcribe(self, audio_path: Path) -> AsrTranscript:
        if self._model is None:
            raise AsrBackendFailure(
                stage="asr_model",
                code="asr_model_not_prepared",
                message="The local faster-whisper model was not prepared for this run.",
                retryable=False,
            )
        try:
            raw_segments, info = self._model.transcribe(
                str(audio_path),
                task="transcribe",
                beam_size=5,
                vad_filter=True,
            )
            segments = _validated_asr_segments(raw_segments)
            _validate_transcript_text_quality(segments)
            language = _optional_text(getattr(info, "language", None))
        except AsrBackendFailure:
            raise
        except Exception as error:
            raise _classify_transcription_failure(error) from error
        if not segments:
            raise AsrBackendFailure(
                stage="transcription",
                code="asr_empty_transcript",
                message="Local faster-whisper did not produce usable timed speech segments.",
                retryable=False,
            )
        return AsrTranscript(segments=segments, language=language)


class LocalAsrSource:
    name = "local_faster_whisper"
    success_stage = "transcription"

    def __init__(
        self,
        *,
        audio_backend: AudioBackend,
        transcriber: LocalTranscriber,
        model_directory: Path,
        temporary_root: Path,
        allow_model_download: bool = False,
        keep_audio: bool = False,
        free_space_bytes: Callable[[Path], int] | None = None,
        model_notice: Callable[[AsrModelNotice], None] | None = None,
    ) -> None:
        if allow_model_download and model_notice is None:
            raise ValueError("A model notice callback is required before model downloads")
        self._audio_backend = audio_backend
        self._transcriber = transcriber
        self._model_directory = model_directory
        self._temporary_root = temporary_root
        self._allow_model_download = allow_model_download
        self._keep_audio = keep_audio
        self._free_space_bytes = free_space_bytes or _free_space_bytes
        self._model_notice = model_notice

    def fetch(self, request: VideoRequest) -> EvidenceBundle:
        fallback = _fallback_metadata(request)
        try:
            probe = self._transcriber.probe()
        except AsrBackendFailure as probe_failure:
            return _failure_evidence(fallback, probe_failure)
        if not probe.available_locally and not self._allow_model_download:
            return _failure_evidence(
                fallback,
                AsrBackendFailure(
                    stage="asr_model",
                    code="asr_model_required",
                    message=_model_required_message(probe),
                    retryable=False,
                ),
            )
        try:
            metadata = self._audio_backend.inspect(request)
        except AsrBackendFailure as metadata_failure:
            return _failure_evidence(fallback, metadata_failure)

        space_failure = self._space_failure(metadata, probe)
        if space_failure is not None:
            return _failure_evidence(metadata, space_failure)
        try:
            self._temporary_root.mkdir(parents=True, exist_ok=True)
            run_directory = Path(
                tempfile.mkdtemp(prefix="video-digest-asr-", dir=self._temporary_root)
            )
        except OSError as error:
            return _failure_evidence(
                metadata,
                AsrBackendFailure(
                    stage="preflight",
                    code="temporary_directory_unavailable",
                    message=(
                        "The configured local ASR temporary directory could not be created. "
                        "Choose a writable local directory and retry."
                    ),
                    retryable=False,
                    stderr_summary=sanitize_external_diagnostic(
                        f"{type(error).__name__}: {error}"
                    ),
                ),
            )
        media_locator = run_directory.name
        if not probe.available_locally:
            if self._model_notice is None:
                raise RuntimeError("The ASR model download notice callback is missing")
            self._model_notice(
                AsrModelNotice(
                    model_name=probe.model_name,
                    estimated_download_bytes=probe.estimated_download_bytes,
                    device=probe.device,
                    compute_type=probe.compute_type,
                )
            )
        try:
            self._transcriber.prepare(allow_download=self._allow_model_download)
        except AsrBackendFailure as prepare_failure:
            cleanup_status, media_retained = self._finish_media(run_directory)
            return _failure_evidence(
                metadata,
                prepare_failure,
                media_retained=media_retained,
                media_cleanup_status=cleanup_status,
                media_locator=(
                    media_locator if media_retained or cleanup_status == "cleanup_failed" else None
                ),
            )
        audio_path: Path | None = None
        transcript: AsrTranscript | None = None
        run_failure: AsrBackendFailure | None = None
        try:
            audio_path = self._audio_backend.download(request, run_directory)
            if not _is_within(audio_path, run_directory):
                raise AsrBackendFailure(
                    stage="audio",
                    code="audio_artifact_outside_run",
                    message="The audio backend returned an artifact outside this run directory.",
                    retryable=False,
                )
            transcript = self._transcriber.transcribe(audio_path)
        except AsrBackendFailure as error:
            run_failure = error

        generated_files = tuple(path for path in run_directory.rglob("*") if path.is_file())
        media_downloaded = bool(generated_files) or audio_path is not None
        cleanup_status, media_retained = self._finish_media(run_directory)
        if cleanup_status == "cleanup_failed" and run_failure is None:
            run_failure = AsrBackendFailure(
                stage="cleanup",
                code="media_cleanup_failed",
                message="The current-run temporary audio could not be deleted safely.",
                retryable=True,
            )
        if run_failure is not None:
            return _failure_evidence(
                metadata,
                run_failure,
                media_downloaded=media_downloaded,
                media_retained=media_retained,
                media_cleanup_status=cleanup_status,
                media_locator=(
                    media_locator if media_retained or cleanup_status == "cleanup_failed" else None
                ),
            )
        if transcript is None:
            raise RuntimeError("Local ASR completed without a transcript or failure")
        return EvidenceBundle(
            metadata=metadata,
            segments=transcript.segments,
            transcript_source=self.name,
            transcript_language=transcript.language,
            transcript_is_generated=True,
            completeness="complete",
            media_downloaded=media_downloaded,
            media_kind="audio",
            media_retained=media_retained,
            media_cleanup_status=cleanup_status,
            media_locator=media_locator if media_retained else None,
            data_sent_to_cloud=False,
        )

    def _space_failure(
        self,
        metadata: VideoMetadata,
        probe: AsrModelProbe,
    ) -> AsrBackendFailure | None:
        audio_bytes = _estimated_audio_working_bytes(metadata.duration_seconds)
        model_bytes = 0
        if not probe.available_locally:
            model_bytes = max(probe.estimated_download_bytes * 2, _MODEL_DOWNLOAD_MARGIN_BYTES)
        required = audio_bytes + model_bytes
        target = self._temporary_root
        if model_bytes and _drive_key(self._model_directory) != _drive_key(self._temporary_root):
            model_free = self._free_space_bytes(self._model_directory)
            if model_free < model_bytes:
                return _disk_failure(model_free, model_bytes, "ASR model")
            required = audio_bytes
        available = self._free_space_bytes(target)
        if available < required:
            return _disk_failure(available, required, "temporary audio and ASR working data")
        return None

    def _finish_media(self, run_directory: Path) -> tuple[str, bool]:
        if self._keep_audio:
            return "retained", True
        try:
            shutil.rmtree(run_directory)
        except OSError:
            return "cleanup_failed", True
        return "deleted", False


class _WhisperSegment(Protocol):
    start: float
    end: float
    text: str


class _WhisperInfo(Protocol):
    language: str


class _WhisperModel(Protocol):
    def transcribe(
        self,
        audio: str,
        *,
        task: str,
        beam_size: int,
        vad_filter: bool,
    ) -> tuple[Iterable[_WhisperSegment], _WhisperInfo]: ...


def _validated_asr_segments(
    raw_segments: Iterable[_WhisperSegment],
) -> tuple[TranscriptSegment, ...]:
    segments: list[TranscriptSegment] = []
    for item in raw_segments:
        start = _finite_number(getattr(item, "start", None))
        end = _finite_number(getattr(item, "end", None))
        text = _optional_text(getattr(item, "text", None))
        if start is None or end is None or start < 0 or end <= start or text is None:
            continue
        segments.append(
            TranscriptSegment(start_seconds=start, end_seconds=end, text=text)
        )
    if any(
        current.start_seconds >= following.start_seconds
        for current, following in pairwise(segments)
    ):
        raise AsrBackendFailure(
            stage="transcription",
            code="asr_timing_invalid",
            message="Local faster-whisper returned non-monotonic segment timestamps.",
            retryable=False,
        )
    return tuple(segments)


def _validate_transcript_text_quality(segments: tuple[TranscriptSegment, ...]) -> None:
    if any("\ufffd" in segment.text for segment in segments):
        raise AsrBackendFailure(
            stage="transcription",
            code="asr_transcript_quality_failed",
            message=(
                "Local faster-whisper returned corrupted replacement characters. The "
                "transcript is not safe to summarize; retry with a larger model or a clearer "
                "audio source."
            ),
            retryable=False,
        )


def _classify_audio_failure(
    stage: str,
    output: str,
    exit_status: int,
) -> AsrBackendFailure:
    normalized = output.casefold()
    if _has_cookie_lock_signal(normalized):
        code = "cookie_database_locked"
        message = (
            "The browser Cookie database is locked. Use a connected-browser path or close "
            "the browser before explicitly retrying the local audio adapter."
        )
        retryable = True
    elif "no module named yt_dlp" in normalized:
        code = "dependency_missing"
        message = "The yt-dlp dependency is missing from the Skill environment."
        retryable = False
    elif "429" in normalized or "too many requests" in normalized:
        code = "rate_limited"
        message = "The platform rate-limited the audio-only request."
        retryable = True
    elif any(marker in normalized for marker in ("sign in", "login required", "log in")):
        code = "authentication_required"
        message = "The audio-only request requires an authenticated local browser session."
        retryable = False
    elif any(marker in normalized for marker in ("403", "412", "forbidden")):
        code = "site_blocked"
        message = "The platform blocked the audio-only request."
        retryable = True
    elif any(
        marker in normalized
        for marker in ("connection reset", "temporarily unavailable", "http error 5")
    ):
        code = "temporary_failure"
        message = "The platform audio-only request failed temporarily."
        retryable = True
    else:
        code = "audio_download_failed" if stage == "audio" else "audio_metadata_failed"
        message = f"The local ASR {stage} operation failed."
        retryable = True
    return AsrBackendFailure(
        stage=stage,
        code=code,
        message=message,
        retryable=retryable,
        exit_status=exit_status,
        stderr_summary=sanitize_external_diagnostic(output),
    )


def _has_cookie_lock_signal(value: str) -> bool:
    return (
        any(
            marker in value
            for marker in (
                "could not copy chrome cookie database",
                "database is locked",
                "cookie database is locked",
                "permission denied",
            )
        )
        and "cookie" in value
    )


def _classify_transcription_failure(error: Exception) -> AsrBackendFailure:
    diagnostic = f"{type(error).__name__}: {error}"
    normalized = diagnostic.casefold()
    decode_markers = (
        "decode",
        "invalid data",
        "averror",
        "invaliddataerror",
        "could not open input",
        "ffmpeg",
    )
    if any(marker in normalized for marker in decode_markers):
        code = "audio_decode_failed"
        message = "The downloaded audio could not be decoded locally."
        retryable = False
    else:
        code = "asr_transcription_failed"
        message = "Local faster-whisper transcription failed."
        retryable = True
    return AsrBackendFailure(
        stage="transcription",
        code=code,
        message=message,
        retryable=retryable,
        stderr_summary=sanitize_external_diagnostic(diagnostic),
    )


def _dependency_failure() -> AsrBackendFailure:
    return AsrBackendFailure(
        stage="asr_dependency",
        code="asr_dependency_missing",
        message=(
            "Local ASR dependencies are not installed. Run "
            "`uv sync --extra asr --directory <skill-directory>` and retry."
        ),
        retryable=False,
    )


def _model_required_message(probe: AsrModelProbe) -> str:
    size = _format_mib(probe.estimated_download_bytes)
    return (
        f"The local faster-whisper model '{probe.model_name}' is not installed. "
        f"Its expected download is about {size}; local transcription with "
        f"device={probe.device} and compute_type={probe.compute_type} also consumes processing "
        "time and working disk space. Review this cost, then retry with "
        "--allow-asr-model-download to authorize the first download."
    )


def _validate_execution_configuration(
    ctranslate2: Any,
    *,
    device: str,
    compute_type: str,
) -> None:
    try:
        if device == "cuda" and ctranslate2.get_cuda_device_count() < 1:
            raise AsrBackendFailure(
                stage="asr_device",
                code="asr_cuda_unavailable",
                message=(
                    "CUDA local ASR was requested, but CTranslate2 did not detect a usable "
                    "CUDA device. Use --asr-device cpu or repair the local NVIDIA runtime."
                ),
                retryable=False,
            )
        if compute_type == "auto":
            return
        supported = ctranslate2.get_supported_compute_types(device)
    except AsrBackendFailure:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise AsrBackendFailure(
            stage="asr_device",
            code="asr_device_probe_failed",
            message=(
                "The configured local ASR device could not be inspected. Repair the local "
                "CTranslate2 runtime or use --asr-device cpu."
            ),
            retryable=False,
            stderr_summary=sanitize_external_diagnostic(
                f"{type(error).__name__}: {error}"
            ),
        ) from error
    if compute_type not in supported:
        choices = ", ".join(sorted(str(value) for value in supported)) or "none"
        raise AsrBackendFailure(
            stage="asr_device",
            code="asr_compute_type_unsupported",
            message=(
                f"compute_type={compute_type} is not supported on device={device}. "
                f"Supported local compute types: {choices}."
            ),
            retryable=False,
        )


def _model_files_are_complete(directory: Path) -> bool:
    return all((directory / name).is_file() for name in _REQUIRED_MODEL_FILES) and any(
        (directory / name).is_file() for name in _VOCABULARY_MODEL_FILES
    )


def _estimated_model_bytes(model_name: str) -> int:
    return _MODEL_DOWNLOAD_BYTES.get(model_name, _MODEL_DOWNLOAD_BYTES["small"])


def _estimated_audio_working_bytes(duration_seconds: int | None) -> int:
    duration = duration_seconds if duration_seconds is not None else 60 * 60
    estimated_audio = duration * 64 * 1024
    return max(estimated_audio, _AUDIO_WORKING_MARGIN_BYTES)


def _disk_failure(available: int, required: int, purpose: str) -> AsrBackendFailure:
    return AsrBackendFailure(
        stage="preflight",
        code="disk_space_insufficient",
        message=(
            f"Local ASR needs about {_format_mib(required)} free for {purpose}, but only "
            f"about {_format_mib(max(available, 0))} is available. Free space or choose "
            "model and temporary directories on a larger drive before retrying."
        ),
        retryable=False,
    )


def _failure_evidence(
    metadata: VideoMetadata,
    failure: AsrBackendFailure,
    *,
    media_downloaded: bool = False,
    media_retained: bool | None = None,
    media_cleanup_status: str = "not_applicable",
    media_locator: str | None = None,
) -> EvidenceBundle:
    return EvidenceBundle(
        metadata=metadata,
        segments=(),
        transcript_source="local_faster_whisper",
        transcript_language=None,
        transcript_is_generated=True,
        completeness="partial",
        media_downloaded=media_downloaded,
        media_kind="audio" if media_downloaded else None,
        media_retained=media_retained,
        media_cleanup_status=media_cleanup_status,
        media_locator=media_locator,
        data_sent_to_cloud=False,
        failure=DigestFailure(
            stage=failure.stage,
            code=failure.code,
            message=failure.message,
            retryable=failure.retryable,
            exit_status=failure.exit_status,
            stderr_summary=failure.stderr_summary,
        ),
    )


def _fallback_metadata(request: VideoRequest) -> VideoMetadata:
    reference = video_reference(request.url)
    return VideoMetadata(
        platform=reference.platform,
        video_id=reference.video_id,
        title=reference.video_id,
        channel=None,
        duration_seconds=None,
        canonical_url=reference.canonical_url,
    )


def _free_space_bytes(path: Path) -> int:
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return shutil.disk_usage(current).free


def _drive_key(path: Path) -> str:
    return path.resolve(strict=False).anchor.casefold()


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
    except ValueError:
        return False
    return True


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_duration(value: Any) -> int | None:
    number = _finite_number(value)
    if number is None or number <= 0:
        return None
    return round(number)


def _finite_number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _format_mib(value: int) -> str:
    return f"{math.ceil(value / (1024 * 1024)):,} MiB"


_AUDIO_SUFFIXES = frozenset({".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".webm"})
_REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")
_VOCABULARY_MODEL_FILES = ("vocabulary.txt", "vocabulary.json")
_MODEL_DOWNLOAD_BYTES = {
    "tiny": 78_207_087,
    "base": 147_886_409,
    "small": 486_215_847,
    "medium": 1_530_575_217,
    "turbo": 1_621_665_983,
    "large-v3": 3_090_839_273,
}
_AUDIO_WORKING_MARGIN_BYTES = 512 * 1024 * 1024
_MODEL_DOWNLOAD_MARGIN_BYTES = 512 * 1024 * 1024
