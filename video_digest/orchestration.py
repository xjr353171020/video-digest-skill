from __future__ import annotations

from dataclasses import replace
from typing import Protocol
from uuid import uuid4

from .cache import FileEvidenceCache
from .domain import (
    EvidenceArtifact,
    EvidenceAttempt,
    EvidenceAttemptStatus,
    EvidenceBundle,
    EvidenceCacheInfo,
    EvidenceCacheStatus,
    VideoRequest,
)
from .integrity import evidence_content_sha256


class EvidenceSource(Protocol):
    name: str

    def fetch(self, request: VideoRequest) -> EvidenceBundle: ...


class EvidenceOrchestrator:
    def __init__(
        self,
        sources: tuple[EvidenceSource, ...],
        *,
        fallback_source: EvidenceSource | None = None,
        cache: FileEvidenceCache | None = None,
    ) -> None:
        if not sources:
            raise ValueError("At least one video evidence source is required")
        self._sources = sources
        self._fallback_source = fallback_source
        self._cache = cache

    def fetch(self, request: VideoRequest) -> EvidenceBundle:
        run_id = str(uuid4())
        attempts: list[EvidenceAttempt] = []
        latest_evidence: EvidenceBundle | None = None
        latest_blocking_evidence: EvidenceBundle | None = None
        cache_info = None
        cached_candidate: EvidenceBundle | None = None

        if self._cache is not None:
            lookup = self._cache.lookup(request)
            cache_info = lookup.info
            if lookup.info.status is EvidenceCacheStatus.REVALIDATE:
                if lookup.evidence is None:
                    raise RuntimeError("A cache candidate did not include evidence")
                cached_candidate = lookup.evidence
            elif lookup.info.status is EvidenceCacheStatus.INVALID:
                attempts.append(
                    EvidenceAttempt(
                        source="cache",
                        stage="cache",
                        status=EvidenceAttemptStatus.FAILED,
                        code="cache_invalid",
                        message=lookup.info.basis,
                        retryable=False,
                    )
                )

        for source in self._sources:
            evidence = source.fetch(request)
            latest_evidence = evidence
            attempt = _attempt(source.name, evidence, success_stage="subtitles")
            attempts.append(attempt)
            if attempt.status is EvidenceAttemptStatus.SUCCEEDED:
                if self._cache is not None:
                    if cached_candidate is not None:
                        confirmation = self._cache.confirm_current(
                            request,
                            cached_candidate,
                            evidence,
                        )
                        if confirmation.status is EvidenceCacheStatus.HIT:
                            attempts.append(_cache_attempt(confirmation, succeeded=True))
                            return replace(
                                cached_candidate,
                                run_id=run_id,
                                attempts=tuple(attempts),
                                artifacts=(
                                    _transcript_artifact(run_id, "cache", cached_candidate),
                                ),
                                cache=confirmation,
                            )
                        attempts.append(_cache_attempt(confirmation, succeeded=False))
                    cache_info = self._cache.store(request, evidence)
                return replace(
                    evidence,
                    run_id=run_id,
                    attempts=tuple(attempts),
                    artifacts=(_transcript_artifact(run_id, source.name, evidence),),
                    cache=cache_info,
                )
            if attempt.status is EvidenceAttemptStatus.FAILED:
                latest_blocking_evidence = evidence

        if self._fallback_source is not None and _fallback_is_allowed(attempts):
            source = self._fallback_source
            evidence = source.fetch(request)
            latest_evidence = evidence
            success_stage = getattr(source, "success_stage", "transcription")
            if not isinstance(success_stage, str):
                success_stage = "transcription"
            attempt = _attempt(source.name, evidence, success_stage=success_stage)
            attempts.append(attempt)
            if attempt.status is EvidenceAttemptStatus.SUCCEEDED:
                if self._cache is not None:
                    if cached_candidate is not None:
                        confirmation = self._cache.confirm_current(
                            request,
                            cached_candidate,
                            evidence,
                        )
                        if confirmation.status is EvidenceCacheStatus.HIT:
                            attempts.append(_cache_attempt(confirmation, succeeded=True))
                            return replace(
                                cached_candidate,
                                run_id=run_id,
                                attempts=tuple(attempts),
                                artifacts=(
                                    _transcript_artifact(run_id, "cache", cached_candidate),
                                ),
                                cache=confirmation,
                            )
                        attempts.append(_cache_attempt(confirmation, succeeded=False))
                    cache_info = self._cache.store(request, evidence)
                return replace(
                    evidence,
                    run_id=run_id,
                    attempts=tuple(attempts),
                    artifacts=(_transcript_artifact(run_id, source.name, evidence),),
                    cache=cache_info,
                )
            if attempt.status is EvidenceAttemptStatus.FAILED:
                latest_blocking_evidence = evidence

        if latest_evidence is None:
            raise RuntimeError("The configured video evidence sources did not run")
        if self._cache is not None and cached_candidate is not None:
            cache_info = self._cache.reject_unverified(request)
            attempts.append(_cache_attempt(cache_info, succeeded=False, code="cache_unverified"))
        return replace(
            latest_blocking_evidence or latest_evidence,
            run_id=run_id,
            attempts=tuple(attempts),
            artifacts=(),
            cache=cache_info,
        )


def _attempt(
    source: str,
    evidence: EvidenceBundle,
    *,
    success_stage: str,
) -> EvidenceAttempt:
    failure = evidence.failure
    if evidence.completeness == "complete" and evidence.segments and failure is None:
        return EvidenceAttempt(
            source=source,
            stage=success_stage,
            status=EvidenceAttemptStatus.SUCCEEDED,
            code=None,
            message=None,
            retryable=False,
            exit_status=None,
            stderr_summary=None,
        )
    if failure is None:
        return EvidenceAttempt(
            source=source,
            stage="subtitles",
            status=EvidenceAttemptStatus.FAILED,
            code="incomplete_evidence",
            message="The evidence source returned incomplete transcript evidence.",
            retryable=False,
            exit_status=None,
            stderr_summary=None,
        )
    status = (
        EvidenceAttemptStatus.UNAVAILABLE
        if failure.code in _UNAVAILABLE_CODES
        else EvidenceAttemptStatus.FAILED
    )
    return EvidenceAttempt(
        source=source,
        stage=failure.stage,
        status=status,
        code=failure.code,
        message=failure.message,
        retryable=failure.retryable,
        exit_status=failure.exit_status,
        stderr_summary=failure.stderr_summary,
    )


_UNAVAILABLE_CODES = frozenset(
    {
        "captions_unavailable",
        "chrome_transcript_unavailable",
        "no_transcript_found",
        "source_not_configured",
    }
)


def _fallback_is_allowed(attempts: list[EvidenceAttempt]) -> bool:
    source_attempts = tuple(attempt for attempt in attempts if attempt.source != "cache")
    return bool(source_attempts) and all(
        attempt.status is EvidenceAttemptStatus.UNAVAILABLE for attempt in source_attempts
    )


YouTubeEvidenceOrchestrator = EvidenceOrchestrator


def _transcript_artifact(
    run_id: str,
    source: str,
    evidence: EvidenceBundle,
) -> EvidenceArtifact:
    return EvidenceArtifact(
        run_id=run_id,
        artifact_id=f"{run_id}:transcript",
        kind="transcript",
        source=source,
        content_sha256=evidence_content_sha256(evidence),
        complete=True,
    )


def _cache_attempt(
    info: EvidenceCacheInfo,
    *,
    succeeded: bool,
    code: str | None = None,
) -> EvidenceAttempt:
    return EvidenceAttempt(
        source="cache",
        stage="cache",
        status=(EvidenceAttemptStatus.SUCCEEDED if succeeded else EvidenceAttemptStatus.FAILED),
        code=code or ("cache_hit" if succeeded else "cache_changed"),
        message=info.basis,
        retryable=False,
    )
