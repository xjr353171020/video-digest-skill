from __future__ import annotations

import html
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlparse

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


class YouTubeGateway(Protocol):
    def load_player(self, video_id: str) -> dict[str, Any]: ...

    def load_caption(self, track_url: str) -> dict[str, Any]: ...


class YouTubeGatewayFailure(RuntimeError):
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


class YtDlpBackend(Protocol):
    def inspect(self, video_url: str) -> dict[str, Any]: ...

    def fetch_caption(
        self,
        video_url: str,
        language_code: str,
        *,
        is_generated: bool,
    ) -> dict[str, Any]: ...


class YtDlpCommandRunner(Protocol):
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]: ...


class DefaultYtDlpCommandRunner:
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


class SubprocessYtDlpBackend:
    def __init__(
        self,
        *,
        timeout_seconds: float = 90.0,
        runner: YtDlpCommandRunner | None = None,
        temporary_root: Path | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._runner = runner or DefaultYtDlpCommandRunner()
        self._temporary_root = temporary_root

    def inspect(self, video_url: str) -> dict[str, Any]:
        completed = self._run(
            [
                *self._base_command(),
                "--dump-single-json",
                video_url,
            ],
            stage="metadata",
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise YouTubeGatewayFailure(
                stage="metadata",
                code="metadata_parse_failed",
                message="yt-dlp returned metadata in an unexpected format. Update the lockfile and retry.",
                retryable=True,
                exit_status=completed.returncode,
                stderr_summary=sanitize_external_diagnostic(completed.stderr),
            ) from error
        if not isinstance(payload, dict):
            raise YouTubeGatewayFailure(
                stage="metadata",
                code="metadata_parse_failed",
                message="yt-dlp returned metadata in an unexpected format. Update the lockfile and retry.",
                retryable=True,
                exit_status=completed.returncode,
                stderr_summary=sanitize_external_diagnostic(completed.stderr),
            )
        return payload

    def fetch_caption(
        self,
        video_url: str,
        language_code: str,
        *,
        is_generated: bool,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(
            prefix="video-digest-caption-",
            dir=self._temporary_root,
        ) as temporary_directory:
            output_template = str(Path(temporary_directory) / "%(id)s.%(ext)s")
            subtitle_flag = "--write-auto-subs" if is_generated else "--write-subs"
            completed = self._run(
                [
                    *self._base_command(),
                    subtitle_flag,
                    "--sub-langs",
                    language_code,
                    "--sub-format",
                    "json3",
                    "--output",
                    output_template,
                    video_url,
                ],
                stage="subtitles",
            )
            files = tuple(path for path in Path(temporary_directory).rglob("*") if path.is_file())
            media_files = tuple(path for path in files if path.suffix.lower() in _MEDIA_SUFFIXES)
            if media_files:
                raise YouTubeGatewayFailure(
                    stage="subtitles",
                    code="unexpected_media_download",
                    message="The subtitle-only command unexpectedly produced a media file.",
                    retryable=False,
                    exit_status=completed.returncode,
                    stderr_summary=sanitize_external_diagnostic(completed.stderr),
                )
            caption_files = tuple(path for path in files if path.name.endswith(".json3"))
            if not caption_files:
                return {"events": []}
            if len(caption_files) != 1:
                raise YouTubeGatewayFailure(
                    stage="subtitles",
                    code="caption_ambiguous",
                    message="The subtitle-only command produced more than one caption track.",
                    retryable=False,
                    exit_status=completed.returncode,
                    stderr_summary=sanitize_external_diagnostic(completed.stderr),
                )
            try:
                payload = json.loads(caption_files[0].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise YouTubeGatewayFailure(
                    stage="subtitles",
                    code="caption_parse_failed",
                    message="The selected caption track could not be parsed as JSON3.",
                    retryable=True,
                    exit_status=completed.returncode,
                    stderr_summary=sanitize_external_diagnostic(completed.stderr),
                ) from error
            if not isinstance(payload, dict):
                raise YouTubeGatewayFailure(
                    stage="subtitles",
                    code="caption_parse_failed",
                    message="The selected caption track could not be parsed as JSON3.",
                    retryable=True,
                    exit_status=completed.returncode,
                    stderr_summary=sanitize_external_diagnostic(completed.stderr),
                )
            return payload

    def _base_command(self) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--skip-download",
            "--ignore-no-formats-error",
            "--quiet",
            "--no-warnings",
        ]
        if shutil.which("node") is not None:
            command.extend(("--js-runtimes", "node"))
        return command

    def _run(self, command: list[str], *, stage: str) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._runner.run(command, self._timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise YouTubeGatewayFailure(
                stage=stage,
                code="timeout",
                message=f"yt-dlp timed out during the {stage} stage. Retry later.",
                retryable=True,
            ) from error
        if completed.returncode != 0:
            raise _classify_ytdlp_failure(
                stage,
                completed.stdout + "\n" + completed.stderr,
                completed.returncode,
            )
        return completed


class YtDlpYouTubeGateway:
    def __init__(self, backend: YtDlpBackend) -> None:
        self._backend = backend

    def load_player(self, video_id: str) -> dict[str, Any]:
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        info = self._backend.inspect(video_url)
        resolved_video_id = str(info.get("id") or video_id)
        return {
            "videoDetails": {
                "videoId": resolved_video_id,
                "title": str(info.get("title") or resolved_video_id),
                "author": info.get("channel") or info.get("uploader"),
                "lengthSeconds": info.get("duration"),
            },
            "captions": {
                "playerCaptionsTracklistRenderer": {
                    "captionTracks": _tracks_from_ytdlp(info, resolved_video_id),
                }
            },
        }

    def load_caption(self, track_url: str) -> dict[str, Any]:
        parsed = urlparse(track_url)
        query = parse_qs(parsed.query)
        video_id = parsed.path.strip("/")
        language_code = query.get("language", [""])[0]
        is_generated = query.get("generated", ["0"])[0] == "1"
        if parsed.scheme != "yt-dlp" or parsed.netloc != "caption":
            raise ValueError("The caption track is not a yt-dlp track")
        if not video_id or not language_code:
            raise ValueError("The yt-dlp caption track is incomplete")
        return self._backend.fetch_caption(
            f"https://www.youtube.com/watch?v={video_id}",
            language_code,
            is_generated=is_generated,
        )


class YouTubeTranscriptAdapter:
    def __init__(
        self,
        gateway: YouTubeGateway,
        *,
        source_name: str = "youtube_caption",
    ) -> None:
        self._gateway = gateway
        self.name = source_name

    def fetch(self, request: VideoRequest) -> EvidenceBundle:
        video_id = youtube_video_id(request.url)
        try:
            player = self._gateway.load_player(video_id)
        except YouTubeGatewayFailure as failure:
            return _failed_evidence(
                metadata=VideoMetadata(
                    video_id=video_id,
                    title=video_id,
                    channel=None,
                    duration_seconds=None,
                    canonical_url=f"https://www.youtube.com/watch?v={video_id}",
                ),
                failure=failure,
            )
        details = player["videoDetails"]
        tracks = player["captions"]["playerCaptionsTracklistRenderer"]["captionTracks"]
        metadata = VideoMetadata(
            video_id=str(details.get("videoId") or video_id),
            title=str(details.get("title") or video_id),
            channel=_optional_text(details.get("author")),
            duration_seconds=_optional_int(details.get("lengthSeconds")),
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        )
        if not tracks:
            return EvidenceBundle(
                metadata=metadata,
                segments=(),
                transcript_source="none",
                transcript_language=None,
                transcript_is_generated=False,
                completeness="partial",
                media_downloaded=False,
                failure=DigestFailure(
                    stage="subtitles",
                    code="captions_unavailable",
                    message="The video does not expose a usable caption track.",
                    retryable=False,
                ),
            )
        track = _choose_track(tracks, request.preferred_languages)
        try:
            caption = self._gateway.load_caption(str(track["baseUrl"]))
        except YouTubeGatewayFailure as failure:
            return _failed_evidence(
                metadata=metadata,
                failure=failure,
                transcript_source=self.name,
                transcript_language=_optional_text(track.get("languageCode")),
                transcript_is_generated=track.get("kind") == "asr",
            )

        segments, has_untimed_text = _segments_from_json3(caption)
        if not segments:
            content_failure = _caption_content_failure(has_untimed_text=has_untimed_text)
            return EvidenceBundle(
                metadata=metadata,
                segments=(),
                transcript_source=self.name,
                transcript_language=_optional_text(track.get("languageCode")),
                transcript_is_generated=track.get("kind") == "asr",
                completeness="partial",
                media_downloaded=False,
                failure=content_failure,
            )
        if has_untimed_text:
            return EvidenceBundle(
                metadata=metadata,
                segments=segments,
                transcript_source=self.name,
                transcript_language=_optional_text(track.get("languageCode")),
                transcript_is_generated=track.get("kind") == "asr",
                completeness="partial",
                media_downloaded=False,
                failure=_caption_content_failure(has_untimed_text=True),
            )
        return EvidenceBundle(
            metadata=metadata,
            segments=segments,
            transcript_source=self.name,
            transcript_language=_optional_text(track.get("languageCode")),
            transcript_is_generated=track.get("kind") == "asr",
            completeness="complete",
            media_downloaded=False,
        )


def youtube_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif host.endswith("youtube.com"):
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    else:
        video_id = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id):
        raise ValueError("The URL does not contain a valid YouTube video ID")
    return video_id


def _choose_track(
    tracks: list[dict[str, Any]], preferred_languages: tuple[str, ...]
) -> dict[str, Any]:
    candidates = tuple(
        CaptionTrack(
            identifier=str(index),
            language_code=str(track.get("languageCode") or ""),
            display_name=str(track.get("name") or track.get("languageCode") or ""),
            is_generated=track.get("kind") == "asr",
            is_original=track.get("isOriginal") is True,
        )
        for index, track in enumerate(tracks)
    )
    selected = choose_caption_track(candidates, preferred_languages)
    return tracks[int(selected.identifier)]


def _segments_from_json3(
    payload: dict[str, Any],
) -> tuple[tuple[TranscriptSegment, ...], bool]:
    segments: list[TranscriptSegment] = []
    has_untimed_text = False
    events = payload.get("events", [])
    if not isinstance(events, list):
        return (), False
    for event in events:
        if not isinstance(event, dict):
            continue
        raw_parts = event.get("segs", [])
        if not isinstance(raw_parts, list):
            continue
        text = "".join(str(part.get("utf8") or "") for part in raw_parts if isinstance(part, dict))
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        if not text:
            continue
        timing = _json3_timing(event)
        if timing is None:
            has_untimed_text = True
            continue
        start, end = timing
        segments.append(
            TranscriptSegment(
                start_seconds=start,
                end_seconds=end,
                text=text,
            )
        )
    return tuple(segments), has_untimed_text


def _json3_timing(event: dict[str, Any]) -> tuple[float, float] | None:
    if "tStartMs" not in event or "dDurationMs" not in event:
        return None
    try:
        start_milliseconds = float(event["tStartMs"])
        duration_milliseconds = float(event["dDurationMs"])
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(start_milliseconds)
        or not math.isfinite(duration_milliseconds)
        or start_milliseconds < 0
        or duration_milliseconds <= 0
    ):
        return None
    start_seconds = start_milliseconds / 1000
    return start_seconds, start_seconds + (duration_milliseconds / 1000)


def _caption_content_failure(*, has_untimed_text: bool) -> DigestFailure:
    if has_untimed_text:
        return DigestFailure(
            stage="subtitles",
            code="caption_timestamps_missing",
            message="Some caption text did not include reliable start and end times.",
            retryable=False,
        )
    return DigestFailure(
        stage="subtitles",
        code="caption_empty",
        message="The selected caption track did not contain usable text.",
        retryable=False,
    )


def _tracks_from_ytdlp(info: dict[str, Any], video_id: str) -> list[dict[str, Any]]:
    tracks: list[dict[str, Any]] = []
    original_language = _infer_ytdlp_original_language(info)
    for field_name, is_generated in (
        ("subtitles", False),
        ("automatic_captions", True),
    ):
        raw_tracks = info.get(field_name)
        if not isinstance(raw_tracks, dict):
            continue
        language_codes = [
            str(language)
            for language, formats in raw_tracks.items()
            if formats and (not is_generated or _is_original_auto_caption(formats))
        ]
        language_codes.sort(
            key=lambda language: (
                language != original_language,
                language.casefold(),
            )
        )
        for language_code in language_codes:
            if not raw_tracks.get(language_code):
                continue
            tracks.append(
                {
                    "baseUrl": _ytdlp_track_url(video_id, language_code, is_generated),
                    "languageCode": language_code,
                    "name": {"simpleText": language_code},
                    "isOriginal": language_code == original_language,
                    **({"kind": "asr"} if is_generated else {}),
                }
            )
    return tracks


def _infer_ytdlp_original_language(info: dict[str, Any]) -> str | None:
    return _optional_text(info.get("language"))


def _is_original_auto_caption(formats: object) -> bool:
    if not isinstance(formats, list):
        return True
    for caption_format in formats:
        if not isinstance(caption_format, dict):
            return True
        url = _optional_text(caption_format.get("url"))
        if url is None or "tlang" not in parse_qs(urlparse(url).query):
            return True
    return False


def _failed_evidence(
    *,
    metadata: VideoMetadata,
    failure: YouTubeGatewayFailure,
    transcript_source: str = "none",
    transcript_language: str | None = None,
    transcript_is_generated: bool = False,
) -> EvidenceBundle:
    return EvidenceBundle(
        metadata=metadata,
        segments=(),
        transcript_source=transcript_source,
        transcript_language=transcript_language,
        transcript_is_generated=transcript_is_generated,
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


def _ytdlp_track_url(video_id: str, language_code: str, is_generated: bool) -> str:
    query = urlencode(
        {
            "language": language_code,
            "generated": "1" if is_generated else "0",
        }
    )
    return f"yt-dlp://caption/{video_id}?{query}"


def _classify_ytdlp_failure(
    stage: str,
    output: str,
    exit_status: int,
) -> YouTubeGatewayFailure:
    normalized = output.casefold()
    stderr_summary = sanitize_external_diagnostic(output)
    if "no module named yt_dlp" in normalized:
        return YouTubeGatewayFailure(
            stage=stage,
            code="dependency_missing",
            message="yt-dlp is unavailable. Run uv sync in the skill directory and retry.",
            retryable=False,
            exit_status=exit_status,
            stderr_summary=stderr_summary,
        )
    if "http error 429" in normalized or "too many requests" in normalized:
        return YouTubeGatewayFailure(
            stage=stage,
            code="rate_limited",
            message="YouTube rate-limited the request. Wait before retrying.",
            retryable=True,
            exit_status=exit_status,
            stderr_summary=stderr_summary,
        )
    if "sign in to confirm" in normalized or "login required" in normalized:
        return YouTubeGatewayFailure(
            stage=stage,
            code="authentication_required",
            message="YouTube requires an authenticated browser session for this video.",
            retryable=False,
            exit_status=exit_status,
            stderr_summary=stderr_summary,
        )
    if "video unavailable" in normalized or "private video" in normalized:
        return YouTubeGatewayFailure(
            stage=stage,
            code="video_unavailable",
            message="The YouTube video is unavailable or access-restricted.",
            retryable=False,
            exit_status=exit_status,
            stderr_summary=stderr_summary,
        )
    return YouTubeGatewayFailure(
        stage=stage,
        code="external_tool_failed",
        message=f"yt-dlp could not complete the {stage} stage. Update dependencies and retry.",
        retryable=True,
        exit_status=exit_status,
        stderr_summary=stderr_summary,
    )


_MEDIA_SUFFIXES = frozenset(
    {
        ".m4a",
        ".mkv",
        ".mov",
        ".mp3",
        ".mp4",
        ".opus",
        ".webm",
    }
)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    text = _optional_text(value)
    return int(text) if text is not None else None
