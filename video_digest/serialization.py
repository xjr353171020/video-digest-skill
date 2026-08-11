from __future__ import annotations

from typing import Any

from .domain import DigestFailure, EvidenceBundle, VideoRequest


def evidence_document(request: VideoRequest, evidence: EvidenceBundle) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": evidence.completeness,
        "request": {
            "url": request.url,
            "focus": request.focus,
            "preferred_languages": list(request.preferred_languages),
        },
        "evidence": {
            "metadata": {
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
            "failure": _failure_document(evidence.failure),
        },
    }


def failed_request_document(request: VideoRequest, message: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": "failed",
        "request": {
            "url": request.url,
            "focus": request.focus,
            "preferred_languages": list(request.preferred_languages),
        },
        "evidence": None,
        "failure": {
            "stage": "request",
            "code": "unsupported_url",
            "message": message,
            "retryable": False,
        },
    }


def _failure_document(failure: DigestFailure | None) -> dict[str, Any] | None:
    if failure is None:
        return None
    return {
        "stage": failure.stage,
        "code": failure.code,
        "message": failure.message,
        "retryable": failure.retryable,
    }
