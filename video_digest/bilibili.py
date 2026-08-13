from __future__ import annotations

import html
import json
import math
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

from .caption_selection import choose_caption_track
from .diagnostics import sanitize_external_diagnostic
from .domain import (
    CaptionTrack,
    DigestFailure,
    EvidenceBundle,
    TranscriptSegment,
    VideoMetadata,
    VideoRequest,
)
from .video_urls import VideoReference, bilibili_video_reference


class BilibiliCommandRunner(Protocol):
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]: ...


class DefaultBilibiliCommandRunner:
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


class BilibiliGatewayFailure(RuntimeError):
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


class SubprocessBilibiliBackend:
    def __init__(
        self,
        *,
        timeout_seconds: float = 90.0,
        runner: BilibiliCommandRunner | None = None,
        temporary_root: Path | None = None,
        cookies_from_browser: str | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._runner = runner or DefaultBilibiliCommandRunner()
        self._temporary_root = temporary_root
        self._cookies_from_browser = cookies_from_browser

    def list_tracks(self, reference: VideoReference) -> tuple[CaptionTrack, ...]:
        completed = self._run(
            [*self._base_command(), "--list-subs", reference.canonical_url],
            stage="subtitles",
        )
        combined = f"{completed.stdout}\n{completed.stderr}"
        if _has_authentication_signal(combined):
            raise BilibiliGatewayFailure(
                stage="subtitles",
                code="authentication_required",
                message=(
                    "Bilibili reports that native subtitles require a logged-in browser "
                    "session or an explicitly authorized local browser-cookie adapter."
                ),
                retryable=False,
                exit_status=completed.returncode,
                stderr_summary=sanitize_external_diagnostic(combined),
            )
        return _tracks_from_list_output(completed.stdout)

    def inspect(self, reference: VideoReference) -> VideoMetadata:
        completed = self._run(
            [
                *self._base_command(quiet=True),
                "--no-warnings",
                "--dump-single-json",
                reference.canonical_url,
            ],
            stage="metadata",
        )
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise BilibiliGatewayFailure(
                stage="metadata",
                code="metadata_parse_failed",
                message="The Bilibili metadata response had an unexpected format.",
                retryable=True,
                exit_status=completed.returncode,
                stderr_summary=sanitize_external_diagnostic(completed.stderr),
            ) from error
        if not isinstance(value, dict):
            raise BilibiliGatewayFailure(
                stage="metadata",
                code="metadata_parse_failed",
                message="The Bilibili metadata response had an unexpected format.",
                retryable=True,
                exit_status=completed.returncode,
                stderr_summary=sanitize_external_diagnostic(completed.stderr),
            )
        return VideoMetadata(
            platform="bilibili",
            video_id=reference.video_id,
            title=_optional_text(value.get("title")) or reference.video_id,
            channel=_optional_text(value.get("uploader")) or _optional_text(value.get("channel")),
            duration_seconds=_optional_duration(value.get("duration")),
            canonical_url=reference.canonical_url,
        )

    def fetch_caption(
        self,
        reference: VideoReference,
        track: CaptionTrack,
    ) -> tuple[TranscriptSegment, ...]:
        with tempfile.TemporaryDirectory(
            prefix="video-digest-bilibili-caption-",
            dir=self._temporary_root,
        ) as temporary_directory:
            output_template = str(Path(temporary_directory) / "%(id)s.%(ext)s")
            completed = self._run(
                [
                    *self._base_command(quiet=True),
                    "--no-warnings",
                    "--write-subs",
                    "--sub-langs",
                    track.identifier,
                    "--sub-format",
                    "srt",
                    "--output",
                    output_template,
                    reference.canonical_url,
                ],
                stage="subtitles",
            )
            files = tuple(path for path in Path(temporary_directory).rglob("*") if path.is_file())
            media_files = tuple(path for path in files if path.suffix.lower() in _MEDIA_SUFFIXES)
            if media_files:
                raise BilibiliGatewayFailure(
                    stage="subtitles",
                    code="unexpected_media_download",
                    message="The subtitle-only Bilibili command produced a media file.",
                    retryable=False,
                    exit_status=completed.returncode,
                    stderr_summary=sanitize_external_diagnostic(completed.stderr),
                )
            caption_files = tuple(path for path in files if path.suffix.lower() == ".srt")
            if not caption_files:
                return ()
            if len(caption_files) != 1:
                raise BilibiliGatewayFailure(
                    stage="subtitles",
                    code="caption_ambiguous",
                    message="The Bilibili subtitle command produced more than one selected track.",
                    retryable=False,
                    exit_status=completed.returncode,
                    stderr_summary=sanitize_external_diagnostic(completed.stderr),
                )
            try:
                return _segments_from_srt(caption_files[0].read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, ValueError) as error:
                raise BilibiliGatewayFailure(
                    stage="subtitles",
                    code="caption_parse_failed",
                    message="The selected Bilibili subtitle track could not be parsed.",
                    retryable=True,
                    exit_status=completed.returncode,
                    stderr_summary=sanitize_external_diagnostic(completed.stderr),
                ) from error

    def _base_command(self, *, quiet: bool = False) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--skip-download",
            "--ignore-no-formats-error",
            "--no-colors",
        ]
        if quiet:
            command.append("--quiet")
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
            raise BilibiliGatewayFailure(
                stage=stage,
                code="timeout",
                message=f"The Bilibili {stage} request timed out. Retry later.",
                retryable=True,
            ) from error
        if completed.returncode != 0:
            raise _classify_bilibili_failure(
                stage,
                f"{completed.stdout}\n{completed.stderr}",
                completed.returncode,
            )
        return completed


class BilibiliYtDlpSource:
    name = "bilibili_yt_dlp"

    def __init__(self, backend: SubprocessBilibiliBackend) -> None:
        self._backend = backend

    def fetch(self, request: VideoRequest) -> EvidenceBundle:
        reference = bilibili_video_reference(request.url)
        fallback = _fallback_metadata(reference)
        try:
            tracks = self._backend.list_tracks(reference)
        except BilibiliGatewayFailure as failure:
            return _failed_evidence(fallback, failure)
        if not tracks:
            return _failure_evidence(
                fallback,
                code="captions_unavailable",
                message="The Bilibili video does not expose a native subtitle track.",
                retryable=False,
            )
        track = choose_caption_track(tracks, request.preferred_languages)
        try:
            metadata = self._backend.inspect(reference)
            segments = self._backend.fetch_caption(reference, track)
        except BilibiliGatewayFailure as failure:
            return _failed_evidence(
                fallback,
                failure,
                language=track.language_code,
                is_generated=track.is_generated,
            )
        if not segments:
            return _failure_evidence(
                metadata,
                code="caption_empty",
                message="The selected Bilibili subtitle track contained no usable timed text.",
                retryable=False,
                language=track.language_code,
                is_generated=track.is_generated,
            )
        return EvidenceBundle(
            metadata=metadata,
            segments=segments,
            transcript_source=self.name,
            transcript_language=track.language_code,
            transcript_is_generated=track.is_generated,
            completeness="complete",
            media_downloaded=False,
        )


class BilibiliChromeTranscriptFileSource:
    name = "bilibili_browser_transcript"

    def __init__(
        self,
        capture_path: Path | None,
        *,
        expected_capture_id: str | None,
        max_age: timedelta = timedelta(minutes=15),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._capture_path = capture_path
        self._expected_capture_id = expected_capture_id
        self._max_age = max_age
        self._now = now or (lambda: datetime.now(timezone.utc))

    def fetch(self, request: VideoRequest) -> EvidenceBundle:
        reference = bilibili_video_reference(request.url)
        fallback = _fallback_metadata(reference)
        if self._capture_path is None or self._expected_capture_id is None:
            return _failure_evidence(
                fallback,
                stage="bilibili_browser_transcript",
                code="source_not_configured",
                message="No current Bilibili browser subtitle capture was supplied for this run.",
                retryable=False,
                source=self.name,
            )
        try:
            if self._capture_path.stat().st_size > _MAX_CAPTURE_BYTES:
                raise ValueError("capture_too_large")
            value = json.loads(self._capture_path.read_text(encoding="utf-8"))
            document = _required_mapping(value, "capture_document")
            _reject_sensitive_keys(document)
            if document.get("schema_version") != _BROWSER_SCHEMA_VERSION:
                raise ValueError("capture_schema")
            if document.get("platform") != "bilibili":
                raise ValueError("platform")
            if _required_text(document.get("capture_id"), "capture_id") != (
                self._expected_capture_id
            ):
                raise ValueError("capture_id_mismatch")
            captured_at = _parse_captured_at(document.get("captured_at"))
            age = self._now().astimezone(timezone.utc) - captured_at
            if age < timedelta(minutes=-5) or age > self._max_age:
                raise ValueError("capture_stale")
            if (
                _required_text(document.get("bvid"), "bvid").casefold()
                != (reference.bvid or "").casefold()
            ):
                raise ValueError("video_id_mismatch")
            if _required_positive_int(document.get("page"), "page") != reference.page:
                raise ValueError("page_mismatch")
            _required_text(str(document.get("cid") or ""), "cid")
            metadata = _parse_browser_metadata(document.get("metadata"), reference)
            language, is_generated = _parse_browser_track(document.get("track"))
            segments, invalid_timing = _parse_browser_segments(document.get("segments"))
            complete = _required_bool(
                document.get("transcript_complete"),
                "transcript_complete",
            )
        except FileNotFoundError:
            return _failure_evidence(
                fallback,
                stage="bilibili_browser_transcript",
                code="chrome_capture_missing",
                message="The current Bilibili browser subtitle capture file is missing.",
                retryable=True,
                source=self.name,
            )
        except (OSError, TypeError, json.JSONDecodeError, ValueError) as error:
            reason = str(error) if type(error) is ValueError else type(error).__name__
            return _failure_evidence(
                fallback,
                stage="bilibili_browser_transcript",
                code="chrome_capture_invalid",
                message=f"The Bilibili browser subtitle capture failed validation: {reason}.",
                retryable=True,
                source=self.name,
            )
        if not segments:
            return _failure_evidence(
                metadata,
                stage="bilibili_browser_transcript",
                code="caption_empty",
                message="The Bilibili browser capture contained no usable timed subtitle text.",
                retryable=False,
                language=language,
                is_generated=is_generated,
                source=self.name,
            )
        if invalid_timing:
            return _partial_browser_evidence(
                metadata,
                segments,
                language,
                is_generated,
                code="caption_timestamps_missing",
                message="Some Bilibili browser subtitle rows lacked reliable timing.",
            )
        if not complete or not _covers_bilibili_duration(metadata, segments):
            return _partial_browser_evidence(
                metadata,
                segments,
                language,
                is_generated,
                code="chrome_capture_incomplete",
                message=(
                    "The Bilibili browser capture was not proven to cover the full selected "
                    "subtitle track."
                ),
            )
        return EvidenceBundle(
            metadata=metadata,
            segments=segments,
            transcript_source=self.name,
            transcript_language=language,
            transcript_is_generated=is_generated,
            completeness="complete",
            media_downloaded=False,
        )


def _tracks_from_list_output(output: str) -> tuple[CaptionTrack, ...]:
    tracks: list[CaptionTrack] = []
    in_table = False
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.casefold().startswith("language") and "format" in stripped.casefold():
            in_table = True
            continue
        if not in_table or not stripped or stripped.startswith("["):
            continue
        match = re.match(r"(?P<language>\S+)\s+(?P<formats>\S.*)", stripped)
        if match is None:
            continue
        raw_language = match.group("language")
        if raw_language.casefold() == "danmaku":
            continue
        generated = raw_language.casefold().startswith("ai-")
        language = raw_language[3:] if generated else raw_language
        tracks.append(
            CaptionTrack(
                identifier=raw_language,
                language_code=language,
                display_name=raw_language,
                is_generated=generated,
                is_original=not generated,
            )
        )
    return tuple(tracks)


def _segments_from_srt(value: str) -> tuple[TranscriptSegment, ...]:
    segments: list[TranscriptSegment] = []
    for block in re.split(r"\r?\n\s*\r?\n", value.strip()):
        lines = block.splitlines()
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timing = lines[timing_index].split("-->", 1)
        if len(timing) != 2:
            continue
        start = _srt_seconds(timing[0].strip())
        end = _srt_seconds(timing[1].strip().split()[0])
        text = " ".join(lines[timing_index + 1 :])
        text = re.sub(r"<[^>]+>", " ", html.unescape(text))
        text = re.sub(r"\s+", " ", text).strip()
        if start is None or end is None or end <= start or not text:
            continue
        segments.append(TranscriptSegment(start_seconds=start, end_seconds=end, text=text))
    if any(
        current.start_seconds >= following.start_seconds
        for current, following in pairwise(segments)
    ):
        raise ValueError("caption_timing_order")
    return tuple(segments)


def _srt_seconds(value: str) -> float | None:
    match = re.fullmatch(r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{3})", value)
    if match is None:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    milliseconds = int(match.group(4))
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def _classify_bilibili_failure(
    stage: str,
    output: str,
    exit_status: int,
) -> BilibiliGatewayFailure:
    normalized = output.casefold()
    if _has_cookie_lock_signal(normalized):
        code = "cookie_database_locked"
        message = (
            "The browser Cookie database is currently locked. Use the connected browser "
            "subtitle path or close the browser before explicitly retrying the local adapter."
        )
        retryable = True
    elif _has_authentication_signal(normalized):
        code = "authentication_required"
        message = "Bilibili subtitles require a logged-in session for this video."
        retryable = False
    elif "429" in normalized or "too many requests" in normalized:
        code = "rate_limited"
        message = "Bilibili rate-limited the subtitle request. Wait before retrying."
        retryable = True
    elif "412" in normalized or "403" in normalized or "forbidden" in normalized:
        code = "site_blocked"
        message = "Bilibili blocked the subtitle request. Try the connected browser path."
        retryable = True
    elif any(
        marker in normalized
        for marker in ("connection reset", "temporarily unavailable", "http error 5")
    ):
        code = "temporary_failure"
        message = "The Bilibili subtitle request failed temporarily. Retry later."
        retryable = True
    elif "no module named yt_dlp" in normalized:
        code = "dependency_missing"
        message = "The yt-dlp dependency is missing. Synchronize the Skill environment once."
        retryable = True
    else:
        code = "external_command_failed"
        message = f"The Bilibili {stage} command failed."
        retryable = True
    return BilibiliGatewayFailure(
        stage=stage,
        code=code,
        message=message,
        retryable=retryable,
        exit_status=exit_status,
        stderr_summary=sanitize_external_diagnostic(output),
    )


def _has_authentication_signal(value: str) -> bool:
    normalized = value.casefold()
    return any(
        marker in normalized
        for marker in (
            "subtitles are only available when logged in",
            "login required",
            "log in to access",
            "sign in to access",
        )
    )


def _has_cookie_lock_signal(value: str) -> bool:
    normalized = value.casefold()
    return (
        any(
            marker in normalized
            for marker in (
                "could not copy chrome cookie database",
                "database is locked",
                "cookie database is locked",
                "permission denied",
            )
        )
        and "cookie" in normalized
    )


def _parse_browser_metadata(value: Any, reference: VideoReference) -> VideoMetadata:
    metadata = _required_mapping(value, "metadata")
    channel = metadata.get("channel")
    if channel is not None and not isinstance(channel, str):
        raise TypeError("channel")
    duration = _required_positive_int(metadata.get("duration_seconds"), "duration_seconds")
    return VideoMetadata(
        platform="bilibili",
        video_id=reference.video_id,
        title=_required_text(metadata.get("title"), "title"),
        channel=channel,
        duration_seconds=duration,
        canonical_url=reference.canonical_url,
    )


def _parse_browser_track(value: Any) -> tuple[str, bool]:
    track = _required_mapping(value, "track")
    language = _required_text(track.get("language_code"), "language_code")
    generated = track.get("is_generated")
    if not isinstance(generated, bool):
        raise TypeError("is_generated")
    return language, generated


def _parse_browser_segments(value: Any) -> tuple[tuple[TranscriptSegment, ...], bool]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("segments")
    segments: list[TranscriptSegment] = []
    invalid_timing = False
    for item in value:
        segment = _required_mapping(item, "segment")
        text_value = segment.get("text")
        if not isinstance(text_value, str):
            raise TypeError("segment_text")
        text = re.sub(r"\s+", " ", html.unescape(text_value)).strip()
        if not text:
            continue
        start = _number(segment.get("start_seconds"))
        end = _number(segment.get("end_seconds"))
        if start is None or end is None or start < 0 or end <= start:
            invalid_timing = True
            continue
        segments.append(TranscriptSegment(start_seconds=start, end_seconds=end, text=text))
    return tuple(segments), invalid_timing


def _covers_bilibili_duration(
    metadata: VideoMetadata,
    segments: tuple[TranscriptSegment, ...],
) -> bool:
    duration = metadata.duration_seconds
    if duration is None:
        return False
    if duration > 60 and len(segments) < 2:
        return False
    start_tolerance = max(30.0, duration * 0.05)
    end_tolerance = max(60.0, duration * 0.10)
    if segments[0].start_seconds > start_tolerance:
        return False
    if not duration - end_tolerance <= segments[-1].end_seconds <= duration + 5:
        return False
    if any(
        segment.end_seconds - segment.start_seconds > _MAX_CAPTURE_SEGMENT_SECONDS
        for segment in segments
    ):
        return False
    return all(
        current.start_seconds < following.start_seconds
        and current.end_seconds - following.start_seconds <= 1.0
        for current, following in pairwise(segments)
    )


def _partial_browser_evidence(
    metadata: VideoMetadata,
    segments: tuple[TranscriptSegment, ...],
    language: str,
    is_generated: bool,
    *,
    code: str,
    message: str,
) -> EvidenceBundle:
    return EvidenceBundle(
        metadata=metadata,
        segments=segments,
        transcript_source="bilibili_browser_transcript",
        transcript_language=language,
        transcript_is_generated=is_generated,
        completeness="partial",
        media_downloaded=False,
        failure=DigestFailure(
            stage="bilibili_browser_transcript",
            code=code,
            message=message,
            retryable=True,
        ),
    )


def _failed_evidence(
    metadata: VideoMetadata,
    failure: BilibiliGatewayFailure,
    *,
    language: str | None = None,
    is_generated: bool = False,
) -> EvidenceBundle:
    return EvidenceBundle(
        metadata=metadata,
        segments=(),
        transcript_source="bilibili_yt_dlp",
        transcript_language=language,
        transcript_is_generated=is_generated,
        completeness="partial",
        media_downloaded=False,
        failure=DigestFailure(
            stage=failure.stage,
            code=failure.code,
            message=failure.message,
            retryable=failure.retryable,
            exit_status=failure.exit_status,
            stderr_summary=failure.stderr_summary,
        ),
    )


def _failure_evidence(
    metadata: VideoMetadata,
    *,
    code: str,
    message: str,
    retryable: bool,
    stage: str = "subtitles",
    language: str | None = None,
    is_generated: bool = False,
    source: str = "bilibili_yt_dlp",
) -> EvidenceBundle:
    return EvidenceBundle(
        metadata=metadata,
        segments=(),
        transcript_source=source,
        transcript_language=language,
        transcript_is_generated=is_generated,
        completeness="partial",
        media_downloaded=False,
        failure=DigestFailure(
            stage=stage,
            code=code,
            message=message,
            retryable=retryable,
        ),
    )


def _fallback_metadata(reference: VideoReference) -> VideoMetadata:
    return VideoMetadata(
        platform="bilibili",
        video_id=reference.video_id,
        title=reference.video_id,
        channel=None,
        duration_seconds=None,
        canonical_url=reference.canonical_url,
    )


def _parse_captured_at(value: Any) -> datetime:
    text = _required_text(value, "captured_at")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("captured_at_timezone")
    return parsed.astimezone(timezone.utc)


def _reject_sensitive_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).replace("-", "_").casefold()
            if any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS):
                raise ValueError("sensitive_field_rejected")
            _reject_sensitive_keys(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _reject_sensitive_keys(child)


def _required_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(field)
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field)
    return value.strip()


def _required_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(field)
    return value


def _required_positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(field)
    return value


def _number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_duration(value: Any) -> int | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    duration = float(value)
    if not math.isfinite(duration) or duration <= 0:
        return None
    return round(duration)


_BROWSER_SCHEMA_VERSION = 2
_MAX_CAPTURE_BYTES = 30 * 1024 * 1024
_MAX_CAPTURE_SEGMENT_SECONDS = 120.0
_MEDIA_SUFFIXES = frozenset(
    {
        ".3gp",
        ".aac",
        ".flac",
        ".m4a",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
    }
)
_SENSITIVE_KEY_MARKERS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "password",
        "secret",
        "session",
        "signed_url",
        "subtitle_url",
        "token",
    }
)
