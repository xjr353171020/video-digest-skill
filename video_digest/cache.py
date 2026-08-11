from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .domain import (
    EvidenceBundle,
    EvidenceCacheInfo,
    EvidenceCacheStatus,
    TranscriptSegment,
    VideoMetadata,
    VideoRequest,
)
from .integrity import evidence_content_sha256
from .youtube import youtube_video_id


@dataclass(frozen=True)
class CacheLookup:
    info: EvidenceCacheInfo
    evidence: EvidenceBundle | None


class FileEvidenceCache:
    def __init__(
        self,
        directory: Path,
        *,
        ttl: timedelta = timedelta(hours=24),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._directory = directory
        self._ttl = ttl
        self._now = now or (lambda: datetime.now(timezone.utc))

    def lookup(self, request: VideoRequest) -> CacheLookup:
        fingerprint = _request_fingerprint(request)
        key = _cache_key(fingerprint)
        path = self._directory / f"{key}.json"
        if not path.is_file():
            return CacheLookup(
                info=EvidenceCacheInfo(
                    status=EvidenceCacheStatus.MISS,
                    key=key,
                    basis="No cache entry matched the normalized video request.",
                ),
                evidence=None,
            )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            evidence, content_version = self._validate_document(
                document,
                key=key,
                fingerprint=fingerprint,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            reason = str(error) if type(error) is ValueError else type(error).__name__
            return CacheLookup(
                info=EvidenceCacheInfo(
                    status=EvidenceCacheStatus.INVALID,
                    key=key,
                    basis=f"Cache integrity validation failed: {reason}.",
                ),
                evidence=None,
            )
        return CacheLookup(
            info=EvidenceCacheInfo(
                status=EvidenceCacheStatus.REVALIDATE,
                key=key,
                basis=(
                    "Local request fingerprint, adapter version, complete evidence, TTL, "
                    "track identity, and stored content hash matched; a current source must "
                    "confirm the track and content version."
                ),
                content_version=content_version,
            ),
            evidence=evidence,
        )

    def confirm_current(
        self,
        request: VideoRequest,
        cached: EvidenceBundle,
        current: EvidenceBundle,
    ) -> EvidenceCacheInfo:
        key = _cache_key(_request_fingerprint(request))
        if not _is_complete_evidence(current):
            return EvidenceCacheInfo(
                status=EvidenceCacheStatus.INVALID,
                key=key,
                basis="A current source did not return complete evidence for cache revalidation.",
            )
        if _track_identity(cached) != _track_identity(current):
            return EvidenceCacheInfo(
                status=EvidenceCacheStatus.INVALID,
                key=key,
                basis="The current selected subtitle track no longer matches the cached track.",
                content_version=evidence_content_sha256(current),
            )
        cached_version = evidence_content_sha256(cached)
        current_version = evidence_content_sha256(current)
        if cached_version != current_version:
            return EvidenceCacheInfo(
                status=EvidenceCacheStatus.INVALID,
                key=key,
                basis="The current subtitle content version no longer matches the cache.",
                content_version=current_version,
            )
        return EvidenceCacheInfo(
            status=EvidenceCacheStatus.HIT,
            key=key,
            basis=(
                "A current source confirmed the cached subtitle track identity and content "
                "version after local adapter, request, TTL, completeness, and hash checks."
            ),
            content_version=current_version,
        )

    def reject_unverified(self, request: VideoRequest) -> EvidenceCacheInfo:
        return EvidenceCacheInfo(
            status=EvidenceCacheStatus.INVALID,
            key=_cache_key(_request_fingerprint(request)),
            basis=(
                "The locally valid cache candidate was not used because no current source "
                "confirmed its subtitle track and content version."
            ),
        )

    def store(self, request: VideoRequest, evidence: EvidenceBundle) -> EvidenceCacheInfo:
        fingerprint = _request_fingerprint(request)
        key = _cache_key(fingerprint)
        if not _is_complete_evidence(evidence):
            return EvidenceCacheInfo(
                status=EvidenceCacheStatus.INVALID,
                key=key,
                basis="Incomplete evidence was not stored in the cache.",
            )
        content_version = evidence_content_sha256(evidence)
        document = {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "cache_key": key,
            "adapter_version": _ADAPTER_VERSION,
            "created_at": self._now().astimezone(timezone.utc).isoformat(),
            "request_fingerprint": fingerprint,
            "track": {
                "source": evidence.transcript_source,
                "language": evidence.transcript_language,
                "is_generated": evidence.transcript_is_generated,
            },
            "content_version": content_version,
            "evidence": _evidence_document(evidence),
        }
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            temporary_path = self._directory / f".{key}.{uuid4().hex}.tmp"
            temporary_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(self._directory / f"{key}.json")
        except OSError:
            return EvidenceCacheInfo(
                status=EvidenceCacheStatus.INVALID,
                key=key,
                basis="Complete evidence was produced, but the cache entry could not be written.",
                content_version=content_version,
            )
        return EvidenceCacheInfo(
            status=EvidenceCacheStatus.STORED,
            key=key,
            basis="Complete evidence was stored with its request fingerprint and content hash.",
            content_version=content_version,
        )

    def _validate_document(
        self,
        document: Any,
        *,
        key: str,
        fingerprint: dict[str, Any],
    ) -> tuple[EvidenceBundle, str]:
        if not isinstance(document, dict):
            raise TypeError("cache_document")
        if document.get("schema_version") != _CACHE_SCHEMA_VERSION:
            raise ValueError("schema_version")
        if document.get("cache_key") != key:
            raise ValueError("cache_key")
        if document.get("adapter_version") != _ADAPTER_VERSION:
            raise ValueError("adapter_version")
        if document.get("request_fingerprint") != fingerprint:
            raise ValueError("request_fingerprint")
        created_at = document.get("created_at")
        if not isinstance(created_at, str):
            raise TypeError("created_at")
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            raise ValueError("created_at_timezone")
        age = self._now().astimezone(timezone.utc) - created.astimezone(timezone.utc)
        if age < timedelta(minutes=-5) or age > self._ttl:
            raise ValueError("cache_ttl")
        evidence = _parse_evidence(document.get("evidence"))
        if evidence.metadata.video_id != fingerprint["video_id"]:
            raise ValueError("video_id")
        if not _is_complete_evidence(evidence):
            raise ValueError("incomplete_evidence")
        content_version = document.get("content_version")
        if not isinstance(content_version, str):
            raise TypeError("content_version")
        if evidence_content_sha256(evidence) != content_version:
            raise ValueError("content_hash")
        track = document.get("track")
        expected_track = {
            "source": evidence.transcript_source,
            "language": evidence.transcript_language,
            "is_generated": evidence.transcript_is_generated,
        }
        if track != expected_track:
            raise ValueError("track")
        return evidence, content_version


def _request_fingerprint(request: VideoRequest) -> dict[str, Any]:
    return {
        "video_id": youtube_video_id(request.url),
        "preferred_languages": [
            language.strip().replace("_", "-").casefold()
            for language in request.preferred_languages
        ],
    }


def _cache_key(fingerprint: dict[str, Any]) -> str:
    rendered = json.dumps(
        fingerprint,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _is_complete_evidence(evidence: EvidenceBundle) -> bool:
    return (
        evidence.completeness == "complete"
        and evidence.failure is None
        and bool(evidence.segments)
        and evidence.media_downloaded is False
        and all(
            math.isfinite(segment.start_seconds)
            and math.isfinite(segment.end_seconds)
            and segment.start_seconds >= 0
            and segment.end_seconds > segment.start_seconds
            and bool(segment.text.strip())
            for segment in evidence.segments
        )
    )


def _track_identity(evidence: EvidenceBundle) -> tuple[str, str | None, bool]:
    return (
        evidence.transcript_source,
        evidence.transcript_language,
        evidence.transcript_is_generated,
    )


def _evidence_document(evidence: EvidenceBundle) -> dict[str, Any]:
    return {
        "metadata": {
            "video_id": evidence.metadata.video_id,
            "title": evidence.metadata.title,
            "channel": evidence.metadata.channel,
            "duration_seconds": evidence.metadata.duration_seconds,
            "canonical_url": evidence.metadata.canonical_url,
        },
        "segments": [
            {
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
                "text": segment.text,
            }
            for segment in evidence.segments
        ],
        "transcript_source": evidence.transcript_source,
        "transcript_language": evidence.transcript_language,
        "transcript_is_generated": evidence.transcript_is_generated,
        "completeness": evidence.completeness,
        "media_downloaded": evidence.media_downloaded,
    }


def _parse_evidence(value: Any) -> EvidenceBundle:
    if not isinstance(value, dict):
        raise TypeError("evidence")
    metadata_value = value.get("metadata")
    segments_value = value.get("segments")
    if not isinstance(metadata_value, dict) or not isinstance(segments_value, list):
        raise TypeError("evidence_shape")
    video_id = _required_text(metadata_value.get("video_id"), "video_id")
    title = _required_text(metadata_value.get("title"), "title")
    canonical_url = _required_text(metadata_value.get("canonical_url"), "canonical_url")
    channel_value = metadata_value.get("channel")
    if channel_value is not None and not isinstance(channel_value, str):
        raise TypeError("channel")
    duration_value = metadata_value.get("duration_seconds")
    if duration_value is not None and (
        not isinstance(duration_value, int) or isinstance(duration_value, bool)
    ):
        raise TypeError("duration_seconds")
    segments: list[TranscriptSegment] = []
    for segment_value in segments_value:
        if not isinstance(segment_value, dict):
            raise TypeError("segment")
        start = _required_number(segment_value.get("start_seconds"), "start_seconds")
        end = _required_number(segment_value.get("end_seconds"), "end_seconds")
        text = _required_text(segment_value.get("text"), "segment_text")
        segments.append(TranscriptSegment(start_seconds=start, end_seconds=end, text=text))
    source = _required_text(value.get("transcript_source"), "transcript_source")
    language_value = value.get("transcript_language")
    if language_value is not None and not isinstance(language_value, str):
        raise TypeError("transcript_language")
    generated = value.get("transcript_is_generated")
    if not isinstance(generated, bool):
        raise TypeError("transcript_is_generated")
    if value.get("completeness") != "complete" or value.get("media_downloaded") is not False:
        raise ValueError("incomplete_evidence")
    return EvidenceBundle(
        metadata=VideoMetadata(
            video_id=video_id,
            title=title,
            channel=channel_value,
            duration_seconds=duration_value,
            canonical_url=canonical_url,
        ),
        segments=tuple(segments),
        transcript_source=source,
        transcript_language=language_value,
        transcript_is_generated=generated,
        completeness="complete",
        media_downloaded=False,
    )


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field)
    return value


def _required_number(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(field)
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(field)
    return number


_CACHE_SCHEMA_VERSION = 2
_ADAPTER_VERSION = "youtube-evidence-t2-v2"
