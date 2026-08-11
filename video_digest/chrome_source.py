from __future__ import annotations

import html
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

from .domain import (
    DigestFailure,
    EvidenceBundle,
    TranscriptSegment,
    VideoMetadata,
    VideoRequest,
)
from .youtube import youtube_video_id


class ChromeTranscriptFileSource:
    name = "chrome_transcript"

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
        video_id = youtube_video_id(request.url)
        fallback_metadata = _fallback_metadata(video_id)
        if self._capture_path is None or self._expected_capture_id is None:
            return _failure(
                fallback_metadata,
                code="source_not_configured",
                message="No current Chrome transcript capture was supplied for this run.",
                retryable=False,
            )
        try:
            if self._capture_path.stat().st_size > _MAX_CAPTURE_BYTES:
                raise ValueError("capture_too_large")
            value = json.loads(self._capture_path.read_text(encoding="utf-8"))
            document = _required_mapping(value, "capture_document")
            _reject_sensitive_keys(document)
            if document.get("schema_version") != _SCHEMA_VERSION:
                raise ValueError("capture_schema")
            capture_id = _required_text(document.get("capture_id"), "capture_id")
            if capture_id != self._expected_capture_id:
                raise ValueError("capture_id_mismatch")
            captured_at = _parse_captured_at(document.get("captured_at"))
            age = self._now().astimezone(timezone.utc) - captured_at
            if age < timedelta(minutes=-5) or age > self._max_age:
                raise ValueError("capture_stale")
            if _required_text(document.get("video_id"), "video_id") != video_id:
                raise ValueError("video_id_mismatch")
            metadata = _parse_metadata(document.get("metadata"), video_id)
            language, is_generated = _parse_track(document.get("track"))
            segments, has_invalid_timing = _parse_segments(document.get("segments"))
            transcript_complete = _required_bool(
                document.get("transcript_complete"),
                "transcript_complete",
            )
        except FileNotFoundError:
            return _failure(
                fallback_metadata,
                code="chrome_capture_missing",
                message="The current Chrome transcript capture file is missing.",
                retryable=True,
            )
        except (OSError, TypeError, json.JSONDecodeError, ValueError) as error:
            reason = str(error) if type(error) is ValueError else type(error).__name__
            return _failure(
                fallback_metadata,
                code="chrome_capture_invalid",
                message=f"The Chrome transcript capture failed validation: {reason}.",
                retryable=True,
            )

        if not segments:
            return _failure(
                metadata,
                code="caption_empty",
                message="The current Chrome transcript did not contain usable timed text.",
                retryable=False,
                language=language,
                is_generated=is_generated,
            )
        if not transcript_complete or not _covers_declared_duration(metadata, segments):
            return EvidenceBundle(
                metadata=metadata,
                segments=segments,
                transcript_source=self.name,
                transcript_language=language,
                transcript_is_generated=is_generated,
                completeness="partial",
                media_downloaded=False,
                failure=DigestFailure(
                    stage="chrome_transcript",
                    code="chrome_capture_incomplete",
                    message=(
                        "The Chrome transcript capture was not proven to cover the full "
                        "available transcript. A fresh source will be tried."
                    ),
                    retryable=True,
                ),
            )
        if has_invalid_timing:
            return EvidenceBundle(
                metadata=metadata,
                segments=segments,
                transcript_source=self.name,
                transcript_language=language,
                transcript_is_generated=is_generated,
                completeness="partial",
                media_downloaded=False,
                failure=DigestFailure(
                    stage="chrome_transcript",
                    code="caption_timestamps_missing",
                    message="Some Chrome transcript rows lacked reliable start and end times.",
                    retryable=False,
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


def _parse_metadata(value: Any, video_id: str) -> VideoMetadata:
    metadata = _required_mapping(value, "metadata")
    title_value = metadata.get("title")
    title = _required_text(title_value, "title") if title_value is not None else video_id
    channel_value = metadata.get("channel")
    if channel_value is not None and not isinstance(channel_value, str):
        raise TypeError("channel")
    duration_value = metadata.get("duration_seconds")
    if duration_value is not None and (
        not isinstance(duration_value, int) or isinstance(duration_value, bool)
    ):
        raise TypeError("duration_seconds")
    if duration_value is not None and duration_value <= 0:
        raise ValueError("duration_seconds")
    return VideoMetadata(
        video_id=video_id,
        title=title,
        channel=channel_value,
        duration_seconds=duration_value,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _parse_track(value: Any) -> tuple[str, bool]:
    track = _required_mapping(value, "track")
    language = _required_text(track.get("language_code"), "language_code")
    is_generated = track.get("is_generated")
    if not isinstance(is_generated, bool):
        raise TypeError("is_generated")
    return language, is_generated


def _parse_segments(value: Any) -> tuple[tuple[TranscriptSegment, ...], bool]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("segments")
    segments: list[TranscriptSegment] = []
    has_invalid_timing = False
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
            has_invalid_timing = True
            continue
        segments.append(
            TranscriptSegment(
                start_seconds=start,
                end_seconds=end,
                text=text,
            )
        )
    return tuple(segments), has_invalid_timing


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


def _parse_captured_at(value: Any) -> datetime:
    text = _required_text(value, "captured_at")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("captured_at_timezone")
    return parsed.astimezone(timezone.utc)


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


def _number(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _covers_declared_duration(
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
        and abs(current.end_seconds - following.start_seconds) <= 0.25
        for current, following in pairwise(segments)
    )


def _failure(
    metadata: VideoMetadata,
    *,
    code: str,
    message: str,
    retryable: bool,
    language: str | None = None,
    is_generated: bool = False,
) -> EvidenceBundle:
    return EvidenceBundle(
        metadata=metadata,
        segments=(),
        transcript_source="chrome_transcript",
        transcript_language=language,
        transcript_is_generated=is_generated,
        completeness="partial",
        media_downloaded=False,
        failure=DigestFailure(
            stage="chrome_transcript",
            code=code,
            message=message,
            retryable=retryable,
        ),
    )


def _fallback_metadata(video_id: str) -> VideoMetadata:
    return VideoMetadata(
        video_id=video_id,
        title=video_id,
        channel=None,
        duration_seconds=None,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
    )


_SCHEMA_VERSION = 2
_MAX_CAPTURE_BYTES = 20 * 1024 * 1024
_MAX_CAPTURE_SEGMENT_SECONDS = 120.0
_SENSITIVE_KEY_MARKERS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "password",
        "secret",
        "session",
        "token",
    }
)
