from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from video_digest import (
    AsrBackendFailure,
    AsrModelNotice,
    AsrModelProbe,
    AsrTranscript,
    DigestFailure,
    EvidenceAttemptStatus,
    EvidenceBundle,
    EvidenceCacheStatus,
    EvidenceOrchestrator,
    FileEvidenceCache,
    LocalAsrSource,
    TranscriptSegment,
    VideoMetadata,
    VideoRequest,
    YtDlpAudioBackend,
)
from video_digest import asr as asr_module
from video_digest import cli as cli_module
from video_digest.serialization import evidence_document
from video_digest.video_urls import video_reference

_YOUTUBE_URL = "https://www.youtube.com/watch?v=demo123"
_BILIBILI_URL = "https://www.bilibili.com/video/BV1X7411F744?p=5"
_LARGE_FREE_SPACE = 20 * 1024 * 1024 * 1024


class ControlledSource:
    def __init__(self, name: str, evidence: EvidenceBundle) -> None:
        self.name = name
        self._evidence = evidence

    def fetch(self, request: VideoRequest) -> EvidenceBundle:
        reference = video_reference(request.url)
        return replace(
            self._evidence,
            metadata=replace(
                self._evidence.metadata,
                platform=reference.platform,
                video_id=reference.video_id,
                canonical_url=reference.canonical_url,
            ),
        )


class RecordingFallbackSource:
    name = "local_faster_whisper"
    success_stage = "transcription"

    def __init__(self) -> None:
        self.requests: list[VideoRequest] = []

    def fetch(self, request: VideoRequest) -> EvidenceBundle:
        self.requests.append(request)
        reference = video_reference(request.url)
        return EvidenceBundle(
            metadata=VideoMetadata(
                platform=reference.platform,
                video_id=reference.video_id,
                title="Locally transcribed video",
                channel="Local channel",
                duration_seconds=60,
                canonical_url=reference.canonical_url,
            ),
            segments=(
                TranscriptSegment(
                    start_seconds=1.0,
                    end_seconds=4.0,
                    text="Local ASR evidence.",
                ),
            ),
            transcript_source=self.name,
            transcript_language="en",
            transcript_is_generated=True,
            completeness="complete",
            media_downloaded=True,
            media_kind="audio",
            media_retained=False,
            media_cleanup_status="deleted",
            data_sent_to_cloud=False,
        )


class ControlledAudioBackend:
    def __init__(
        self,
        *,
        events: list[str] | None = None,
        download_failure: Exception | None = None,
    ) -> None:
        self.events = events if events is not None else []
        self.download_failure = download_failure
        self.inspect_requests: list[VideoRequest] = []
        self.download_requests: list[VideoRequest] = []

    def inspect(self, request: VideoRequest) -> VideoMetadata:
        self.events.append("inspect")
        self.inspect_requests.append(request)
        reference = video_reference(request.url)
        return VideoMetadata(
            platform=reference.platform,
            video_id=reference.video_id,
            title="A video without captions",
            channel="Demo Channel",
            duration_seconds=300,
            canonical_url=reference.canonical_url,
        )

    def download(self, request: VideoRequest, directory: Path) -> Path:
        self.events.append("download")
        self.download_requests.append(request)
        directory.mkdir(parents=True, exist_ok=True)
        partial_path = directory / "current-run.webm.part"
        if self.download_failure is not None:
            partial_path.write_bytes(b"partial audio")
            raise self.download_failure
        audio_path = directory / "current-run.webm"
        audio_path.write_bytes(b"controlled audio")
        return audio_path


class ControlledTranscriber:
    def __init__(
        self,
        *,
        model_available: bool = True,
        events: list[str] | None = None,
        probe_failure: AsrBackendFailure | None = None,
        transcribe_failure: AsrBackendFailure | None = None,
        device: str = "cpu",
        compute_type: str = "int8",
    ) -> None:
        self.events = events if events is not None else []
        self.model_available = model_available
        self.probe_failure = probe_failure
        self.transcribe_failure = transcribe_failure
        self.device = device
        self.compute_type = compute_type
        self.prepare_arguments: list[bool] = []
        self.transcribed_paths: list[Path] = []

    def probe(self) -> AsrModelProbe:
        self.events.append("probe")
        if self.probe_failure is not None:
            raise self.probe_failure
        return AsrModelProbe(
            model_name="small",
            available_locally=self.model_available,
            estimated_download_bytes=486_215_847,
            device=self.device,
            compute_type=self.compute_type,
        )

    def prepare(self, *, allow_download: bool) -> None:
        self.events.append("prepare")
        self.prepare_arguments.append(allow_download)

    def transcribe(self, audio_path: Path) -> AsrTranscript:
        self.events.append("transcribe")
        assert audio_path.is_file()
        self.transcribed_paths.append(audio_path)
        if self.transcribe_failure is not None:
            raise self.transcribe_failure
        return AsrTranscript(
            segments=(
                TranscriptSegment(
                    start_seconds=0.25,
                    end_seconds=3.75,
                    text="The locally transcribed point.",
                ),
            ),
            language="en",
        )


def _metadata() -> VideoMetadata:
    return VideoMetadata(
        video_id="demo123",
        title="A video without captions",
        channel="Demo Channel",
        duration_seconds=300,
        canonical_url=_YOUTUBE_URL,
    )


def _partial(source: str, code: str) -> EvidenceBundle:
    return EvidenceBundle(
        metadata=_metadata(),
        segments=(),
        transcript_source=source,
        transcript_language=None,
        transcript_is_generated=False,
        completeness="partial",
        media_downloaded=False,
        failure=DigestFailure(
            stage="subtitles",
            code=code,
            message=f"{source}: {code}",
            retryable=code in {"rate_limited", "site_blocked", "timeout"},
        ),
    )


@pytest.mark.parametrize("url", (_YOUTUBE_URL, _BILIBILI_URL))
def test_asr_runs_for_both_platforms_only_after_all_caption_sources_are_unavailable(
    url: str,
) -> None:
    fallback = RecordingFallbackSource()
    orchestrator = EvidenceOrchestrator(
        sources=(
            ControlledSource("browser", _partial("browser", "source_not_configured")),
            ControlledSource("native", _partial("native", "captions_unavailable")),
        ),
        fallback_source=fallback,
    )

    evidence = orchestrator.fetch(VideoRequest(url=url))

    assert fallback.requests == [VideoRequest(url=url)]
    assert evidence.completeness == "complete"
    assert evidence.transcript_source == "local_faster_whisper"
    assert evidence.media_downloaded is True
    assert [attempt.status for attempt in evidence.attempts] == [
        EvidenceAttemptStatus.UNAVAILABLE,
        EvidenceAttemptStatus.UNAVAILABLE,
        EvidenceAttemptStatus.SUCCEEDED,
    ]
    assert evidence.attempts[-1].stage == "transcription"


@pytest.mark.parametrize(
    "blocking_code",
    (
        "authentication_required",
        "rate_limited",
        "site_blocked",
        "timeout",
        "metadata_parse_failed",
        "caption_parse_failed",
        "caption_empty",
    ),
)
def test_asr_does_not_run_after_a_non_unavailable_caption_failure(
    blocking_code: str,
) -> None:
    fallback = RecordingFallbackSource()
    orchestrator = EvidenceOrchestrator(
        sources=(
            ControlledSource("browser", _partial("browser", "source_not_configured")),
            ControlledSource("native", _partial("native", blocking_code)),
        ),
        fallback_source=fallback,
    )

    evidence = orchestrator.fetch(VideoRequest(url=_YOUTUBE_URL))

    assert fallback.requests == []
    assert evidence.failure is not None
    assert evidence.failure.code == blocking_code
    assert [attempt.source for attempt in evidence.attempts] == ["browser", "native"]


def test_missing_asr_dependency_is_reported_before_metadata_or_audio_download(
    tmp_path: Path,
) -> None:
    audio = ControlledAudioBackend()
    transcriber = ControlledTranscriber(
        probe_failure=AsrBackendFailure(
            stage="asr_dependency",
            code="asr_dependency_missing",
            message="Install the local ASR dependency group.",
            retryable=False,
        )
    )
    source = _local_source(tmp_path, audio=audio, transcriber=transcriber)

    evidence = source.fetch(VideoRequest(url=_YOUTUBE_URL))

    assert evidence.failure is not None
    assert evidence.failure.stage == "asr_dependency"
    assert evidence.failure.code == "asr_dependency_missing"
    assert audio.inspect_requests == []
    assert audio.download_requests == []
    assert evidence.media_downloaded is False


def test_missing_model_reports_download_size_before_any_audio_work(tmp_path: Path) -> None:
    audio = ControlledAudioBackend()
    transcriber = ControlledTranscriber(model_available=False)
    source = _local_source(tmp_path, audio=audio, transcriber=transcriber)

    evidence = source.fetch(VideoRequest(url=_YOUTUBE_URL))

    assert evidence.failure is not None
    assert evidence.failure.stage == "asr_model"
    assert evidence.failure.code == "asr_model_required"
    assert "small" in evidence.failure.message
    assert "464 MiB" in evidence.failure.message
    assert "--allow-asr-model-download" in evidence.failure.message
    assert audio.inspect_requests == []
    assert audio.download_requests == []


def test_disk_space_preflight_happens_before_audio_download(tmp_path: Path) -> None:
    audio = ControlledAudioBackend()
    source = _local_source(
        tmp_path,
        audio=audio,
        transcriber=ControlledTranscriber(),
        free_space_bytes=lambda _: 1,
    )

    evidence = source.fetch(VideoRequest(url=_YOUTUBE_URL))

    assert audio.inspect_requests == [VideoRequest(url=_YOUTUBE_URL)]
    assert audio.download_requests == []
    assert evidence.failure is not None
    assert evidence.failure.stage == "preflight"
    assert evidence.failure.code == "disk_space_insufficient"
    assert evidence.media_downloaded is False


def test_explicit_model_download_notice_precedes_prepare_and_audio_download(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    notices: list[AsrModelNotice] = []
    audio = ControlledAudioBackend(events=events)
    transcriber = ControlledTranscriber(model_available=False, events=events)

    def notify(notice: AsrModelNotice) -> None:
        events.append("notice")
        notices.append(notice)

    source = _local_source(
        tmp_path,
        audio=audio,
        transcriber=transcriber,
        allow_model_download=True,
        model_notice=notify,
    )

    evidence = source.fetch(VideoRequest(url=_YOUTUBE_URL))

    assert evidence.completeness == "complete"
    assert notices == [
        AsrModelNotice(
            model_name="small",
            estimated_download_bytes=486_215_847,
            device="cpu",
            compute_type="int8",
        )
    ]
    assert events.index("notice") < events.index("prepare") < events.index("download")
    assert transcriber.prepare_arguments == [True]


def test_model_download_notice_uses_the_selected_execution_configuration(
    tmp_path: Path,
) -> None:
    notices: list[AsrModelNotice] = []
    source = _local_source(
        tmp_path,
        audio=ControlledAudioBackend(),
        transcriber=ControlledTranscriber(
            model_available=False,
            device="cuda",
            compute_type="float16",
        ),
        allow_model_download=True,
        model_notice=notices.append,
    )

    evidence = source.fetch(VideoRequest(url=_YOUTUBE_URL))

    assert evidence.completeness == "complete"
    assert notices[0].device == "cuda"
    assert notices[0].compute_type == "float16"


def test_large_model_layout_and_download_costs_are_model_specific(tmp_path: Path) -> None:
    model_directory = tmp_path / "turbo"
    model_directory.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"):
        (model_directory / name).write_bytes(b"model")

    assert asr_module._model_files_are_complete(model_directory)
    assert asr_module._estimated_model_bytes("medium") == 1_530_575_217
    assert asr_module._estimated_model_bytes("turbo") == 1_621_665_983
    assert asr_module._estimated_model_bytes("large-v3") == 3_090_839_273


def test_cli_keeps_cpu_int8_defaults_and_exposes_an_explicit_gpu_profile() -> None:
    defaults = cli_module._parser().parse_args([_YOUTUBE_URL])
    gpu = cli_module._parser().parse_args(
        [
            _YOUTUBE_URL,
            "--asr-model",
            "turbo",
            "--asr-device",
            "cuda",
            "--asr-compute-type",
            "int8_float16",
        ]
    )

    assert defaults.asr_model == "small"
    assert defaults.asr_device == "cpu"
    assert defaults.asr_compute_type is None
    assert gpu.asr_model == "turbo"
    assert gpu.asr_device == "cuda"
    assert gpu.asr_compute_type == "int8_float16"


class ControlledCTranslate2:
    def __init__(
        self,
        *,
        cuda_devices: int = 1,
        supported: frozenset[str] = frozenset({"int8", "float16"}),
    ) -> None:
        self.cuda_devices = cuda_devices
        self.supported = supported

    def get_cuda_device_count(self) -> int:
        return self.cuda_devices

    def get_supported_compute_types(self, device: str) -> frozenset[str]:
        assert device in {"cpu", "cuda"}
        return self.supported


def test_cuda_preflight_fails_before_any_media_work_when_no_gpu_is_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_cuda(
        ctranslate2: object,
        *,
        device: str,
        compute_type: str,
    ) -> None:
        assert ctranslate2 is not None
        assert device == "cuda"
        assert compute_type == "float16"
        raise AsrBackendFailure(
            stage="asr_device",
            code="asr_cuda_unavailable",
            message="CUDA is unavailable for this local ASR run.",
            retryable=False,
        )

    monkeypatch.setattr(asr_module, "_validate_execution_configuration", unavailable_cuda)
    audio = ControlledAudioBackend()
    source = LocalAsrSource(
        audio_backend=audio,
        transcriber=asr_module.FasterWhisperTranscriber(
            model_name="small",
            model_directory=tmp_path / "models",
            device="cuda",
            compute_type="float16",
        ),
        model_directory=tmp_path / "models",
        temporary_root=tmp_path / "temp",
    )

    evidence = source.fetch(VideoRequest(url=_YOUTUBE_URL))

    assert evidence.failure is not None
    assert evidence.failure.stage == "asr_device"
    assert evidence.failure.code == "asr_cuda_unavailable"
    assert audio.inspect_requests == []
    assert audio.download_requests == []


def test_compute_type_preflight_reports_supported_local_choices() -> None:
    with pytest.raises(AsrBackendFailure) as captured:
        asr_module._validate_execution_configuration(
            ControlledCTranslate2(supported=frozenset({"int8", "float32"})),
            device="cpu",
            compute_type="float16",
        )

    assert captured.value.stage == "asr_device"
    assert captured.value.code == "asr_compute_type_unsupported"
    assert "float32, int8" in captured.value.message


def test_corrupted_replacement_characters_never_reach_the_summary_contract() -> None:
    with pytest.raises(AsrBackendFailure) as captured:
        asr_module._validate_transcript_text_quality(
            (
                TranscriptSegment(
                    start_seconds=0.0,
                    end_seconds=2.0,
                    text="� corrupted local transcript",
                ),
            )
        )

    assert captured.value.stage == "transcription"
    assert captured.value.code == "asr_transcript_quality_failed"


def test_successful_local_asr_returns_timed_local_evidence_and_cleans_exact_audio(
    tmp_path: Path,
) -> None:
    audio = ControlledAudioBackend()
    transcriber = ControlledTranscriber()
    source = _local_source(tmp_path, audio=audio, transcriber=transcriber)

    evidence = source.fetch(VideoRequest(url=_YOUTUBE_URL))

    assert evidence.failure is None
    assert evidence.completeness == "complete"
    assert evidence.segments == (
        TranscriptSegment(
            start_seconds=0.25,
            end_seconds=3.75,
            text="The locally transcribed point.",
        ),
    )
    assert evidence.transcript_source == "local_faster_whisper"
    assert evidence.transcript_language == "en"
    assert evidence.transcript_is_generated is True
    assert evidence.media_downloaded is True
    assert evidence.media_kind == "audio"
    assert evidence.media_retained is False
    assert evidence.media_cleanup_status == "deleted"
    assert evidence.data_sent_to_cloud is False
    assert not tuple((tmp_path / "temp").rglob("*"))
    document = evidence_document(VideoRequest(url=_YOUTUBE_URL), evidence)
    assert document["evidence"]["media"] == {
        "downloaded": True,
        "kind": "audio",
        "retained": False,
        "cleanup_status": "deleted",
        "locator": None,
        "sent_to_cloud": False,
    }


def test_local_asr_bypasses_caption_cache_without_creating_a_stale_entry(
    tmp_path: Path,
) -> None:
    source = _local_source(
        tmp_path,
        audio=ControlledAudioBackend(),
        transcriber=ControlledTranscriber(),
    )
    cache_directory = tmp_path / "cache"
    orchestrator = EvidenceOrchestrator(
        sources=(
            ControlledSource("browser", _partial("browser", "source_not_configured")),
            ControlledSource("native", _partial("native", "captions_unavailable")),
        ),
        fallback_source=source,
        cache=FileEvidenceCache(cache_directory),
    )

    evidence = orchestrator.fetch(VideoRequest(url=_YOUTUBE_URL))

    assert evidence.completeness == "complete"
    assert evidence.cache is not None
    assert evidence.cache.status is EvidenceCacheStatus.BYPASSED
    assert "local ASR" in evidence.cache.basis
    assert not tuple(cache_directory.glob("*.json"))


def test_debug_retention_keeps_only_current_run_audio(tmp_path: Path) -> None:
    temp_root = tmp_path / "temp"
    unrelated = tmp_path / "unrelated.webm"
    unrelated.write_bytes(b"keep me")
    source = _local_source(
        tmp_path,
        audio=ControlledAudioBackend(),
        transcriber=ControlledTranscriber(),
        keep_audio=True,
    )

    evidence = source.fetch(VideoRequest(url=_BILIBILI_URL))

    assert evidence.completeness == "complete"
    assert evidence.media_retained is True
    assert evidence.media_cleanup_status == "retained"
    assert evidence.media_locator is not None
    assert evidence.media_locator.startswith("video-digest-asr-")
    assert "\\" not in evidence.media_locator
    assert "/" not in evidence.media_locator
    assert len(tuple(temp_root.rglob("*.webm"))) == 1
    assert unrelated.read_bytes() == b"keep me"


def test_unwritable_temporary_root_fails_before_model_or_audio_work(tmp_path: Path) -> None:
    blocked_root = tmp_path / "blocked-temporary-root"
    blocked_root.write_text("not a directory", encoding="utf-8")
    audio = ControlledAudioBackend()
    transcriber = ControlledTranscriber()
    source = LocalAsrSource(
        audio_backend=audio,
        transcriber=transcriber,
        model_directory=tmp_path / "models",
        temporary_root=blocked_root,
        free_space_bytes=lambda _: _LARGE_FREE_SPACE,
    )

    evidence = source.fetch(VideoRequest(url=_YOUTUBE_URL))

    assert evidence.failure is not None
    assert evidence.failure.stage == "preflight"
    assert evidence.failure.code == "temporary_directory_unavailable"
    assert transcriber.prepare_arguments == []
    assert audio.download_requests == []
    assert evidence.media_downloaded is False


def test_external_diagnostics_redact_quoted_absolute_windows_paths() -> None:
    from video_digest.diagnostics import sanitize_external_diagnostic

    sanitized = sanitize_external_diagnostic(
        'RuntimeError: failed to load "C:\\Users\\PrivateProfile\\model\\runtime.dll"'
    )

    assert sanitized is not None
    assert "PrivateProfile" not in sanitized
    assert "runtime.dll" not in sanitized
    assert "<redacted-path>" in sanitized


def test_audio_decode_failure_is_staged_and_current_run_audio_is_cleaned(
    tmp_path: Path,
) -> None:
    transcriber = ControlledTranscriber(
        transcribe_failure=AsrBackendFailure(
            stage="transcription",
            code="audio_decode_failed",
            message="The downloaded audio could not be decoded locally.",
            retryable=False,
        )
    )
    source = _local_source(
        tmp_path,
        audio=ControlledAudioBackend(),
        transcriber=transcriber,
    )

    evidence = source.fetch(VideoRequest(url=_YOUTUBE_URL))

    assert evidence.failure is not None
    assert evidence.failure.stage == "transcription"
    assert evidence.failure.code == "audio_decode_failed"
    assert evidence.media_downloaded is True
    assert evidence.media_cleanup_status == "deleted"
    assert not tuple((tmp_path / "temp").rglob("*"))


@pytest.mark.parametrize("url", (_YOUTUBE_URL, _BILIBILI_URL))
def test_ytdlp_audio_backend_requests_bestaudio_without_video_fallback(
    tmp_path: Path,
    url: str,
) -> None:
    runner = SuccessfulAudioRunner()
    backend = YtDlpAudioBackend(
        runner=runner,
        cookies_from_browser="chrome" if "bilibili" in url else None,
    )

    audio_path = backend.download(VideoRequest(url=url), tmp_path)

    assert audio_path.suffix == ".webm"
    command = runner.commands[-1]
    assert command[command.index("--format") + 1] == "bestaudio"
    assert "--skip-download" not in command
    assert "--extract-audio" not in command
    assert "best" not in command
    if "bilibili" in url:
        assert command[command.index("--cookies-from-browser") + 1] == "chrome"
    else:
        assert "--cookies-from-browser" not in command


def test_audio_download_failure_is_redacted_and_partial_artifacts_are_deleted(
    tmp_path: Path,
) -> None:
    runner = SensitiveFailingAudioRunner()
    source = _local_source(
        tmp_path,
        audio=YtDlpAudioBackend(runner=runner),
        transcriber=ControlledTranscriber(),
    )

    request = VideoRequest(url=_YOUTUBE_URL)
    evidence = source.fetch(request)
    serialized = json.dumps(evidence_document(request, evidence), ensure_ascii=False)

    assert evidence.failure is not None
    assert evidence.failure.stage == "audio"
    assert evidence.failure.code == "audio_download_failed"
    assert "private-cookie" not in serialized
    assert "private-token" not in serialized
    assert "signature-secret" not in serialized
    assert evidence.failure.stderr_summary is not None
    assert "youtube.com" not in evidence.failure.stderr_summary
    assert evidence.media_downloaded is True
    assert evidence.media_cleanup_status == "deleted"
    assert not tuple((tmp_path / "temp").rglob("*"))


class SuccessfulAudioRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, command: list[str], timeout_seconds: float) -> CompletedProcess[str]:
        self.commands.append(command)
        assert timeout_seconds == 600.0
        output_template = Path(command[command.index("--output") + 1])
        output_template.parent.joinpath("demo123.webm").write_bytes(b"audio only")
        return CompletedProcess(command, 0, stdout="", stderr="")


class SensitiveFailingAudioRunner:
    def run(self, command: list[str], timeout_seconds: float) -> CompletedProcess[str]:
        if "--dump-single-json" in command:
            return CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "id": "demo123",
                        "title": "A video without captions",
                        "channel": "Demo Channel",
                        "duration": 300,
                    }
                ),
                stderr="",
            )
        output_template = Path(command[command.index("--output") + 1])
        output_template.parent.joinpath("demo123.webm.part").write_bytes(b"partial")
        return CompletedProcess(
            command,
            1,
            stdout="",
            stderr=(
                "ERROR audio fetch failed https://youtube.com/audio?sig=signature-secret "
                "Cookie: SID=private-cookie Authorization: Bearer private-token"
            ),
        )


class CookieLockedAudioRunner:
    def run(self, command: list[str], timeout_seconds: float) -> CompletedProcess[str]:
        return CompletedProcess(
            command,
            1,
            stdout="",
            stderr=(
                "ERROR: Could not copy Chrome cookie database: Permission denied: "
                "C:\\Users\\PrivateProfile\\AppData\\Local\\Google\\Chrome\\User Data\\"
                "Default\\Network\\Cookies Cookie: private-cookie-value"
            ),
        )


def test_asr_cookie_database_lock_is_staged_and_redacted(tmp_path: Path) -> None:
    request = VideoRequest(url=_BILIBILI_URL)
    source = _local_source(
        tmp_path,
        audio=YtDlpAudioBackend(
            runner=CookieLockedAudioRunner(),
            cookies_from_browser="chrome",
        ),
        transcriber=ControlledTranscriber(),
    )

    evidence = source.fetch(request)
    serialized = json.dumps(evidence_document(request, evidence), ensure_ascii=False)

    assert evidence.failure is not None
    assert evidence.failure.stage == "audio_metadata"
    assert evidence.failure.code == "cookie_database_locked"
    assert "PrivateProfile" not in serialized
    assert "private-cookie-value" not in serialized


def _local_source(
    tmp_path: Path,
    *,
    audio: ControlledAudioBackend | YtDlpAudioBackend,
    transcriber: ControlledTranscriber,
    free_space_bytes: Callable[[Path], int] | None = None,
    allow_model_download: bool = False,
    keep_audio: bool = False,
    model_notice: Callable[[AsrModelNotice], None] | None = None,
) -> LocalAsrSource:
    return LocalAsrSource(
        audio_backend=audio,
        transcriber=transcriber,
        model_directory=tmp_path / "models",
        temporary_root=tmp_path / "temp",
        allow_model_download=allow_model_download,
        keep_audio=keep_audio,
        free_space_bytes=free_space_bytes or (lambda _: _LARGE_FREE_SPACE),
        model_notice=model_notice,
    )
