from __future__ import annotations

from typing import Any

from video_digest import (
    DigestRecommendation,
    DigestResult,
    DigestRunStatus,
    DigestWorkflow,
    VideoRequest,
    WatchSegment,
    YouTubeGatewayFailure,
    YouTubeTranscriptAdapter,
    YtDlpYouTubeGateway,
)


class StubYouTubeGateway:
    def load_player(self, video_id: str) -> dict[str, Any]:
        assert video_id == "demo123"
        return {
            "videoDetails": {
                "videoId": "demo123",
                "title": "A concise demo",
                "author": "Demo Channel",
                "lengthSeconds": "600",
            },
            "captions": {
                "playerCaptionsTracklistRenderer": {
                    "captionTracks": [
                        {
                            "baseUrl": "https://captions.example/en",
                            "languageCode": "en",
                            "name": {"simpleText": "English"},
                        }
                    ]
                }
            },
        }

    def load_caption(self, track_url: str) -> dict[str, Any]:
        assert track_url == "https://captions.example/en"
        return {
            "events": [
                {
                    "tStartMs": 0,
                    "dDurationMs": 3_000,
                    "segs": [{"utf8": "The first useful point."}],
                },
                {
                    "tStartMs": 120_000,
                    "dDurationMs": 4_000,
                    "segs": [{"utf8": "The decisive example appears here."}],
                },
            ]
        }


class EvidenceBasedSummarizer:
    def summarize(self, request: VideoRequest, evidence: Any) -> DigestResult:
        assert request.focus == "decision value"
        return DigestResult(
            one_sentence_conclusion=evidence.segments[0].text,
            core_points=(
                evidence.segments[0].text,
                evidence.segments[1].text,
            ),
            watch_segments=(
                WatchSegment(
                    start_seconds=120.0,
                    end_seconds=124.0,
                    reason="The decisive example",
                ),
            ),
            information_density=8,
            worth_watching=7,
            recommendation=DigestRecommendation.SUMMARY_IS_ENOUGH,
            estimated_minutes_saved=8,
        )


class NoCaptionYouTubeGateway:
    def load_player(self, video_id: str) -> dict[str, Any]:
        assert video_id == "demo123"
        return {
            "videoDetails": {
                "videoId": "demo123",
                "title": "A video without captions",
                "author": "Demo Channel",
                "lengthSeconds": "300",
            },
            "captions": {
                "playerCaptionsTracklistRenderer": {
                    "captionTracks": [],
                }
            },
        }

    def load_caption(self, track_url: str) -> dict[str, Any]:
        raise AssertionError(f"caption download must not run: {track_url}")


class FailIfSummarized:
    def summarize(self, request: VideoRequest, evidence: Any) -> DigestResult:
        raise AssertionError(f"summarizer must not run for {request.url}: {evidence}")


class EmptyCaptionYouTubeGateway(StubYouTubeGateway):
    def load_caption(self, track_url: str) -> dict[str, Any]:
        assert track_url == "https://captions.example/en"
        return {"events": []}


class StubYtDlpBackend:
    def inspect(self, video_url: str) -> dict[str, Any]:
        assert video_url == "https://www.youtube.com/watch?v=demo123"
        return {
            "id": "demo123",
            "title": "A concise demo",
            "channel": "Demo Channel",
            "duration": 600,
            "subtitles": {"en": [{"ext": "json3"}]},
            "automatic_captions": {"en": [{"ext": "json3"}]},
        }

    def fetch_caption(
        self,
        video_url: str,
        language_code: str,
        *,
        is_generated: bool,
    ) -> dict[str, Any]:
        assert video_url == "https://www.youtube.com/watch?v=demo123"
        assert language_code == "en"
        assert is_generated is False
        return StubYouTubeGateway().load_caption("https://captions.example/en")


class MissingYtDlpBackend:
    def inspect(self, video_url: str) -> dict[str, Any]:
        raise YouTubeGatewayFailure(
            stage="metadata",
            code="dependency_missing",
            message="Run uv sync in the skill directory to install yt-dlp.",
            retryable=False,
        )

    def fetch_caption(
        self,
        video_url: str,
        language_code: str,
        *,
        is_generated: bool,
    ) -> dict[str, Any]:
        raise AssertionError("caption fetch must not run without yt-dlp")


class TranslationHeavyYtDlpBackend:
    def inspect(self, video_url: str) -> dict[str, Any]:
        return {
            "id": "demo123",
            "title": "An auto-captioned demo",
            "channel": "Demo Channel",
            "duration": 600,
            "language": "en",
            "subtitles": {},
            "automatic_captions": {
                "aa-en": [{"url": "https://captions.example?lang=en&tlang=aa"}],
                "en": [{"url": "https://captions.example?lang=en"}],
                "zh-Hans": [{"url": "https://captions.example?lang=en&tlang=zh-Hans"}],
            },
        }

    def fetch_caption(
        self,
        video_url: str,
        language_code: str,
        *,
        is_generated: bool,
    ) -> dict[str, Any]:
        assert language_code == "en"
        assert is_generated is True
        return StubYouTubeGateway().load_caption("https://captions.example/en")


def test_user_gets_timestamped_digest_without_media_downloads() -> None:
    workflow = DigestWorkflow(
        adapter=YouTubeTranscriptAdapter(StubYouTubeGateway()),
        summarizer=EvidenceBasedSummarizer(),
    )

    run = workflow.run(
        VideoRequest(
            url="https://www.youtube.com/watch?v=demo123",
            focus="decision value",
            preferred_languages=("en",),
        )
    )

    assert run.status is DigestRunStatus.COMPLETED
    assert run.evidence is not None
    assert run.evidence.metadata.title == "A concise demo"
    assert run.evidence.metadata.channel == "Demo Channel"
    assert [(segment.start_seconds, segment.end_seconds) for segment in run.evidence.segments] == [
        (0.0, 3.0),
        (120.0, 124.0),
    ]
    assert run.evidence.transcript_source == "youtube_caption"
    assert run.evidence.media_downloaded is False
    assert run.digest is not None
    assert run.digest.one_sentence_conclusion == "The first useful point."
    assert run.digest.recommendation is DigestRecommendation.SUMMARY_IS_ENOUGH
    assert run.digest.estimated_minutes_saved == 8


def test_user_gets_partial_result_when_captions_are_unavailable() -> None:
    workflow = DigestWorkflow(
        adapter=YouTubeTranscriptAdapter(NoCaptionYouTubeGateway()),
        summarizer=FailIfSummarized(),
    )

    run = workflow.run(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    assert run.status is DigestRunStatus.PARTIAL
    assert run.evidence is not None
    assert run.evidence.metadata.title == "A video without captions"
    assert run.evidence.segments == ()
    assert run.evidence.completeness == "partial"
    assert run.digest is None
    assert run.failure is not None
    assert run.failure.stage == "subtitles"
    assert run.failure.code == "captions_unavailable"
    assert run.failure.retryable is False


def test_user_gets_partial_result_when_caption_track_is_empty() -> None:
    workflow = DigestWorkflow(
        adapter=YouTubeTranscriptAdapter(EmptyCaptionYouTubeGateway()),
        summarizer=FailIfSummarized(),
    )

    run = workflow.run(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    assert run.status is DigestRunStatus.PARTIAL
    assert run.evidence is not None
    assert run.evidence.segments == ()
    assert run.digest is None
    assert run.failure is not None
    assert run.failure.stage == "subtitles"
    assert run.failure.code == "caption_empty"


def test_user_gets_digest_through_the_production_ytdlp_gateway() -> None:
    workflow = DigestWorkflow(
        adapter=YouTubeTranscriptAdapter(YtDlpYouTubeGateway(StubYtDlpBackend())),
        summarizer=EvidenceBasedSummarizer(),
    )

    run = workflow.run(
        VideoRequest(
            url="https://www.youtube.com/watch?v=demo123",
            focus="decision value",
            preferred_languages=("en",),
        )
    )

    assert run.status is DigestRunStatus.COMPLETED
    assert run.evidence is not None
    assert run.evidence.metadata.title == "A concise demo"
    assert run.evidence.transcript_language == "en"
    assert run.evidence.transcript_is_generated is False
    assert run.evidence.media_downloaded is False
    assert run.digest is not None
    assert run.digest.recommendation is DigestRecommendation.SUMMARY_IS_ENOUGH


def test_user_gets_actionable_failure_when_ytdlp_is_missing() -> None:
    workflow = DigestWorkflow(
        adapter=YouTubeTranscriptAdapter(YtDlpYouTubeGateway(MissingYtDlpBackend())),
        summarizer=FailIfSummarized(),
    )

    run = workflow.run(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    assert run.status is DigestRunStatus.PARTIAL
    assert run.evidence is not None
    assert run.evidence.media_downloaded is False
    assert run.digest is None
    assert run.failure is not None
    assert run.failure.stage == "metadata"
    assert run.failure.code == "dependency_missing"
    assert "uv sync" in run.failure.message
    assert run.failure.retryable is False


def test_auto_caption_fallback_uses_the_original_track_not_a_translation() -> None:
    workflow = DigestWorkflow(
        adapter=YouTubeTranscriptAdapter(YtDlpYouTubeGateway(TranslationHeavyYtDlpBackend())),
        summarizer=EvidenceBasedSummarizer(),
    )

    run = workflow.run(
        VideoRequest(
            url="https://www.youtube.com/watch?v=demo123",
            focus="decision value",
            preferred_languages=("fr",),
        )
    )

    assert run.status is DigestRunStatus.COMPLETED
    assert run.evidence is not None
    assert run.evidence.transcript_language == "en"
    assert run.evidence.transcript_is_generated is True
    assert run.evidence.media_downloaded is False
