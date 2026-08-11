from __future__ import annotations

from typing import Protocol

from .domain import DigestResult, DigestRun, DigestRunStatus, EvidenceBundle, VideoRequest


class EvidenceAdapter(Protocol):
    def fetch(self, request: VideoRequest) -> EvidenceBundle: ...


class SummaryGenerator(Protocol):
    def summarize(self, request: VideoRequest, evidence: EvidenceBundle) -> DigestResult: ...


class DigestWorkflow:
    def __init__(self, adapter: EvidenceAdapter, summarizer: SummaryGenerator) -> None:
        self._adapter = adapter
        self._summarizer = summarizer

    def run(self, request: VideoRequest) -> DigestRun:
        evidence = self._adapter.fetch(request)
        if not evidence.segments:
            return DigestRun(
                status=DigestRunStatus.PARTIAL,
                request=request,
                evidence=evidence,
                digest=None,
                failure=evidence.failure,
            )
        digest = self._summarizer.summarize(request, evidence)
        return DigestRun(
            status=DigestRunStatus.COMPLETED,
            request=request,
            evidence=evidence,
            digest=digest,
        )
