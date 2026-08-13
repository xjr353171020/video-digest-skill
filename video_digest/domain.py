from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DigestRunStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class DigestRecommendation(str, Enum):
    WATCH_ALL = "watch_all"
    WATCH_SELECTED = "watch_selected"
    SUMMARY_IS_ENOUGH = "summary_is_enough"
    SKIP = "skip"


class EvidenceAttemptStatus(str, Enum):
    SUCCEEDED = "succeeded"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class EvidenceCacheStatus(str, Enum):
    MISS = "miss"
    REVALIDATE = "revalidation_required"
    HIT = "hit"
    INVALID = "invalid"
    STORED = "stored"


@dataclass(frozen=True)
class VideoRequest:
    url: str
    focus: str | None = None
    preferred_languages: tuple[str, ...] = ("zh-Hans", "zh", "en")


@dataclass(frozen=True)
class VideoMetadata:
    video_id: str
    title: str
    channel: str | None
    duration_seconds: int | None
    canonical_url: str
    platform: str = "youtube"


@dataclass(frozen=True)
class TranscriptSegment:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True)
class CaptionTrack:
    identifier: str
    language_code: str
    display_name: str
    is_generated: bool
    is_original: bool


@dataclass(frozen=True)
class DigestFailure:
    stage: str
    code: str
    message: str
    retryable: bool
    exit_status: int | None = None
    stderr_summary: str | None = None


@dataclass(frozen=True)
class EvidenceAttempt:
    source: str
    stage: str
    status: EvidenceAttemptStatus
    code: str | None
    message: str | None
    retryable: bool
    exit_status: int | None = None
    stderr_summary: str | None = None


@dataclass(frozen=True)
class EvidenceArtifact:
    run_id: str
    artifact_id: str
    kind: str
    source: str
    content_sha256: str
    complete: bool


@dataclass(frozen=True)
class EvidenceCacheInfo:
    status: EvidenceCacheStatus
    key: str
    basis: str
    content_version: str | None = None


@dataclass(frozen=True)
class EvidenceBundle:
    metadata: VideoMetadata
    segments: tuple[TranscriptSegment, ...]
    transcript_source: str
    transcript_language: str | None
    transcript_is_generated: bool
    completeness: str
    media_downloaded: bool
    failure: DigestFailure | None = None
    run_id: str | None = None
    attempts: tuple[EvidenceAttempt, ...] = ()
    artifacts: tuple[EvidenceArtifact, ...] = ()
    cache: EvidenceCacheInfo | None = None


@dataclass(frozen=True)
class WatchSegment:
    start_seconds: float
    end_seconds: float
    reason: str


@dataclass(frozen=True)
class DigestResult:
    one_sentence_conclusion: str
    core_points: tuple[str, ...]
    watch_segments: tuple[WatchSegment, ...]
    information_density: int
    worth_watching: int
    recommendation: DigestRecommendation
    estimated_minutes_saved: int


@dataclass(frozen=True)
class DigestRun:
    status: DigestRunStatus
    request: VideoRequest
    evidence: EvidenceBundle | None
    digest: DigestResult | None
    failure: DigestFailure | None = None
