from __future__ import annotations

from typing import Any

from .diagnostics import sanitize_external_diagnostic
from .domain import (
    DigestFailure,
    EvidenceArtifact,
    EvidenceAttempt,
    EvidenceBundle,
    EvidenceCacheInfo,
    VideoRequest,
)
from .video_urls import video_reference

_EVIDENCE_SCHEMA_VERSION = 3


def evidence_document(request: VideoRequest, evidence: EvidenceBundle) -> dict[str, Any]:
    return {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "status": evidence.completeness,
        "request": {
            "url": video_reference(request.url).canonical_url,
            "focus": request.focus,
            "preferred_languages": list(request.preferred_languages),
        },
        "run": {
            "run_id": evidence.run_id,
            "attempts": [_attempt_document(attempt) for attempt in evidence.attempts],
            "artifacts": [_artifact_document(artifact) for artifact in evidence.artifacts],
            "cache": _cache_document(evidence.cache),
        },
        "evidence": {
            "metadata": {
                "platform": evidence.metadata.platform,
                "video_id": evidence.metadata.video_id,
                "title": evidence.metadata.title,
                "channel": evidence.metadata.channel,
                "duration_seconds": evidence.metadata.duration_seconds,
                "canonical_url": evidence.metadata.canonical_url,
            },
            "segments": [
                {
                    "start_seconds": round(segment.start_seconds, 3),
                    "end_seconds": round(segment.end_seconds, 3),
                    "text": segment.text,
                }
                for segment in evidence.segments
            ],
            "transcript_source": evidence.transcript_source,
            "transcript_language": evidence.transcript_language,
            "transcript_is_generated": evidence.transcript_is_generated,
            "completeness": evidence.completeness,
            "media_downloaded": evidence.media_downloaded,
            "media": {
                "downloaded": evidence.media_downloaded,
                "kind": evidence.media_kind,
                "retained": evidence.media_retained,
                "cleanup_status": evidence.media_cleanup_status,
                "locator": evidence.media_locator,
                "sent_to_cloud": evidence.data_sent_to_cloud,
            },
            "failure": _failure_document(evidence.failure),
        },
    }


def failed_request_document(request: VideoRequest, message: str) -> dict[str, Any]:
    return {
        "schema_version": _EVIDENCE_SCHEMA_VERSION,
        "status": "failed",
        "request": {
            "url": _safe_request_url(request.url),
            "focus": request.focus,
            "preferred_languages": list(request.preferred_languages),
        },
        "evidence": None,
        "run": None,
        "failure": {
            "stage": "request",
            "code": "unsupported_url",
            "message": message,
            "retryable": False,
            "exit_status": None,
            "stderr_summary": None,
        },
    }


def _safe_request_url(url: str) -> str:
    try:
        return video_reference(url).canonical_url
    except ValueError:
        return "<unsupported-url>"


def _failure_document(failure: DigestFailure | None) -> dict[str, Any] | None:
    if failure is None:
        return None
    return {
        "stage": failure.stage,
        "code": failure.code,
        "message": failure.message,
        "retryable": failure.retryable,
        "exit_status": failure.exit_status,
        "stderr_summary": _safe_diagnostic(failure.stderr_summary),
    }


def _attempt_document(attempt: EvidenceAttempt) -> dict[str, Any]:
    return {
        "source": attempt.source,
        "stage": attempt.stage,
        "status": attempt.status.value,
        "code": attempt.code,
        "message": attempt.message,
        "retryable": attempt.retryable,
        "exit_status": attempt.exit_status,
        "stderr_summary": _safe_diagnostic(attempt.stderr_summary),
    }


def _artifact_document(artifact: EvidenceArtifact) -> dict[str, Any]:
    return {
        "run_id": artifact.run_id,
        "artifact_id": artifact.artifact_id,
        "kind": artifact.kind,
        "source": artifact.source,
        "content_sha256": artifact.content_sha256,
        "complete": artifact.complete,
    }


def _cache_document(cache: EvidenceCacheInfo | None) -> dict[str, Any] | None:
    if cache is None:
        return None
    return {
        "status": cache.status.value,
        "key": cache.key,
        "basis": cache.basis,
        "content_version": cache.content_version,
    }


def _safe_diagnostic(value: str | None) -> str | None:
    return sanitize_external_diagnostic(value) if value is not None else None
