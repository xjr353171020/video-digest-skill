from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess
from typing import NoReturn
from urllib.request import Request

from pytest import MonkeyPatch
from typing_extensions import Self
from youtube_transcript_api import RequestBlocked

from video_digest import (
    CaptionTrack,
    ChromeTranscriptFileSource,
    DigestFailure,
    EvidenceAttemptStatus,
    EvidenceBundle,
    EvidenceCacheStatus,
    FileEvidenceCache,
    LightweightYouTubeSource,
    SubprocessYtDlpBackend,
    TranscriptSegment,
    VideoMetadata,
    VideoRequest,
    YouTubeEvidenceOrchestrator,
    YouTubeOEmbedMetadataProvider,
    YouTubeTranscriptAdapter,
    YouTubeTranscriptApiBackend,
    YtDlpYouTubeGateway,
)
from video_digest.serialization import evidence_document


class ControlledSource:
    def __init__(self, name: str, evidence: EvidenceBundle) -> None:
        self.name = name
        self._evidence = evidence

    def fetch(self, request: VideoRequest) -> EvidenceBundle:
        return replace(
            self._evidence,
            metadata=replace(self._evidence.metadata, canonical_url=request.url),
        )


class FailIfUsedSource:
    name = "must_not_run"

    def fetch(self, request: VideoRequest) -> EvidenceBundle:
        raise AssertionError(f"a valid cache must satisfy {request.url}")


class ControlledLightweightBackend:
    def __init__(self) -> None:
        self._fetched = False

    def list_tracks(self, video_id: str) -> tuple[CaptionTrack, ...]:
        assert video_id == "demo123"
        translated_auto_tracks = tuple(
            CaptionTrack(
                identifier=f"auto-{index}",
                language_code=f"x-auto-{index}",
                display_name=f"Automatic translation {index}",
                is_generated=True,
                is_original=False,
            )
            for index in range(25)
        )
        return (
            CaptionTrack(
                identifier="manual-en",
                language_code="en",
                display_name="English",
                is_generated=False,
                is_original=True,
            ),
            CaptionTrack(
                identifier="manual-zh-cn",
                language_code="zh-CN",
                display_name="简体中文",
                is_generated=False,
                is_original=False,
            ),
            *translated_auto_tracks,
        )

    def fetch_caption(
        self,
        video_id: str,
        track: CaptionTrack,
    ) -> tuple[TranscriptSegment, ...]:
        assert self._fetched is False, "only one selected caption track may be fetched"
        self._fetched = True
        assert video_id == "demo123"
        assert track.identifier == "manual-zh-cn"
        return (
            TranscriptSegment(
                start_seconds=10.0,
                end_seconds=14.0,
                text="这是人工中文字幕。",
            ),
        )


class PreferredGeneratedBackend:
    def list_tracks(self, video_id: str) -> tuple[CaptionTrack, ...]:
        return (
            CaptionTrack(
                identifier="manual-ar",
                language_code="ar",
                display_name="Arabic",
                is_generated=False,
                is_original=False,
            ),
            CaptionTrack(
                identifier="auto-en",
                language_code="en",
                display_name="English (auto-generated)",
                is_generated=True,
                is_original=True,
            ),
        )

    def fetch_caption(
        self,
        video_id: str,
        track: CaptionTrack,
    ) -> tuple[TranscriptSegment, ...]:
        assert track.identifier == "auto-en"
        return (
            TranscriptSegment(
                start_seconds=0.0,
                end_seconds=2.0,
                text="Preferred generated English",
            ),
        )


class ControlledLightweightMetadata:
    def fetch(self, video_id: str) -> VideoMetadata:
        assert video_id == "demo123"
        return VideoMetadata(
            video_id=video_id,
            title="Compact public title",
            channel="Compact public channel",
            duration_seconds=600,
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        )

    def original_caption_language(self, video_id: str) -> str | None:
        assert video_id == "demo123"
        return "en"


class ControlledJapaneseOriginalMetadata(ControlledLightweightMetadata):
    def original_caption_language(self, video_id: str) -> str | None:
        assert video_id == "demo123"
        return "ja"


class ControlledHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._payload if size < 0 else self._payload[:size]


class FailingCommandRunner:
    def __init__(self, stderr: str, *, exit_status: int = 1) -> None:
        self._stderr = stderr
        self._exit_status = exit_status

    def run(self, command: list[str], timeout_seconds: float) -> CompletedProcess[str]:
        return CompletedProcess(
            args=command,
            returncode=self._exit_status,
            stdout="",
            stderr=self._stderr,
        )


class SuccessfulInvalidMetadataRunner:
    def run(self, command: list[str], timeout_seconds: float) -> CompletedProcess[str]:
        return CompletedProcess(
            args=command,
            returncode=0,
            stdout="{not-json",
            stderr=(
                "ERROR Authorization: Basic basic-secret\n"
                '{"Cookie":"SID=cookie-secret","apiKey":"camel-secret",'
                '"xApiKey":"x-camel-secret","authToken":"auth-token-secret",'
                '"secretKey":"secret-key-secret"}'
            ),
        )


class SuccessfulInvalidCaptionRunner:
    def run(self, command: list[str], timeout_seconds: float) -> CompletedProcess[str]:
        if "--dump-single-json" in command:
            return CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "id": "demo123",
                        "title": "Current video",
                        "duration": 60,
                        "language": "en",
                        "subtitles": {"en": [{"ext": "json3"}]},
                        "automatic_captions": {},
                    }
                ),
                stderr="",
            )
        output_template = Path(command[command.index("--output") + 1])
        caption_path = Path(
            str(output_template).replace("%(id)s", "demo123").replace("%(ext)s", "en.json3")
        )
        caption_path.write_text("{not-json", encoding="utf-8")
        return CompletedProcess(
            args=command,
            returncode=0,
            stdout="",
            stderr='{"access_token":"caption-secret"}',
        )


class SuccessfulYtDlpWithoutCurrentCaption:
    def run(self, command: list[str], timeout_seconds: float) -> CompletedProcess[str]:
        if "--dump-single-json" in command:
            return CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "id": "demo123",
                        "title": "Current video",
                        "duration": 60,
                        "subtitles": {"en": [{"ext": "json3"}]},
                        "automatic_captions": {},
                    }
                ),
                stderr="",
            )
        return CompletedProcess(args=command, returncode=0, stdout="", stderr="")


class MultiTrackYtDlpBackend:
    def inspect(self, video_url: str) -> dict[str, object]:
        return {
            "id": "demo123",
            "title": "Multilingual video",
            "duration": 600,
            "language": "en",
            "subtitles": {
                "en": [{"ext": "json3"}],
                "zh-CN": [{"ext": "json3"}],
            },
            "automatic_captions": {
                "en": [{"url": "https://captions.example?lang=en"}],
            },
        }

    def fetch_caption(
        self,
        video_url: str,
        language_code: str,
        *,
        is_generated: bool,
    ) -> dict[str, object]:
        assert language_code == "zh-CN"
        assert is_generated is False
        return {
            "events": [
                {
                    "tStartMs": 10_000,
                    "dDurationMs": 4_000,
                    "segs": [{"utf8": "人工简体中文字幕"}],
                }
            ]
        }


class DeclaredOriginalYtDlpBackend:
    def inspect(self, video_url: str) -> dict[str, object]:
        return {
            "id": "demo123",
            "title": "Manual-only multilingual video",
            "duration": 600,
            "language": "ja",
            "subtitles": {
                "en": [{"ext": "json3"}],
                "ja": [{"ext": "json3"}],
            },
            "automatic_captions": {},
        }

    def fetch_caption(
        self,
        video_url: str,
        language_code: str,
        *,
        is_generated: bool,
    ) -> dict[str, object]:
        assert language_code == "ja"
        assert is_generated is False
        return {
            "events": [
                {
                    "tStartMs": 0,
                    "dDurationMs": 2_000,
                    "segs": [{"utf8": "first yt-dlp manual track"}],
                }
            ]
        }


class BlockedTranscriptApi:
    def list(self, video_id: str) -> NoReturn:
        raise RequestBlocked(video_id)


class ControlledTranscriptSnippet:
    def __init__(self, text: str) -> None:
        self.text = text
        self.start = 0.0
        self.duration = 2.0


class ControlledTranscriptTrack:
    def __init__(self, language: str, language_code: str, text: str) -> None:
        self.language = language
        self.language_code = language_code
        self.is_generated = False
        self._text = text

    def fetch(self) -> tuple[ControlledTranscriptSnippet, ...]:
        return (ControlledTranscriptSnippet(self._text),)


class ManualTranscriptApi:
    def list(self, video_id: str) -> tuple[ControlledTranscriptTrack, ...]:
        return (
            ControlledTranscriptTrack("English", "en", "later translated track"),
            ControlledTranscriptTrack("Japanese", "ja", "verified original track"),
        )


def _metadata() -> VideoMetadata:
    return VideoMetadata(
        video_id="demo123",
        title="A concise demo",
        channel="Demo Channel",
        duration_seconds=600,
        canonical_url="https://www.youtube.com/watch?v=demo123",
    )


def _unavailable(source: str, code: str) -> EvidenceBundle:
    return EvidenceBundle(
        metadata=_metadata(),
        segments=(),
        transcript_source=source,
        transcript_language=None,
        transcript_is_generated=False,
        completeness="partial",
        media_downloaded=False,
        failure=DigestFailure(
            stage="subtitles",
            code=code,
            message=f"{source} did not provide a transcript.",
            retryable=False,
        ),
    )


def _complete(
    source: str,
    *,
    language: str = "en",
    text: str = "The useful point.",
) -> EvidenceBundle:
    return EvidenceBundle(
        metadata=_metadata(),
        segments=(
            TranscriptSegment(
                start_seconds=4.0,
                end_seconds=8.0,
                text=text,
            ),
        ),
        transcript_source=source,
        transcript_language=language,
        transcript_is_generated=False,
        completeness="complete",
        media_downloaded=False,
    )


def test_user_sees_chrome_lightweight_then_ytdlp_fallback_order() -> None:
    orchestrator = YouTubeEvidenceOrchestrator(
        sources=(
            ControlledSource(
                "chrome_transcript",
                _unavailable("chrome_transcript", "chrome_transcript_unavailable"),
            ),
            ControlledSource(
                "youtube_transcript_api",
                _unavailable("youtube_transcript_api", "request_blocked"),
            ),
            ControlledSource("yt_dlp", _complete("yt_dlp")),
        )
    )

    evidence = orchestrator.fetch(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    assert evidence.completeness == "complete"
    assert evidence.transcript_source == "yt_dlp"
    assert [attempt.source for attempt in evidence.attempts] == [
        "chrome_transcript",
        "youtube_transcript_api",
        "yt_dlp",
    ]
    assert [attempt.status for attempt in evidence.attempts] == [
        EvidenceAttemptStatus.UNAVAILABLE,
        EvidenceAttemptStatus.FAILED,
        EvidenceAttemptStatus.SUCCEEDED,
    ]
    assert evidence.run_id is not None


def test_user_gets_one_manual_zh_cn_track_for_zh_hans_preference() -> None:
    orchestrator = YouTubeEvidenceOrchestrator(
        sources=(LightweightYouTubeSource(ControlledLightweightBackend()),)
    )

    evidence = orchestrator.fetch(
        VideoRequest(
            url="https://www.youtube.com/watch?v=demo123",
            preferred_languages=("zh-Hans", "zh", "en"),
        )
    )

    assert evidence.completeness == "complete"
    assert evidence.transcript_source == "youtube_transcript_api"
    assert evidence.transcript_language == "zh-CN"
    assert evidence.transcript_is_generated is False
    assert [segment.text for segment in evidence.segments] == ["这是人工中文字幕。"]


def test_user_gets_redacted_ytdlp_rate_limit_diagnostics() -> None:
    backend = SubprocessYtDlpBackend(
        runner=FailingCommandRunner(
            "ERROR: HTTP Error 429: Too Many Requests "
            "https://www.youtube.com/api?sig=super-secret "
            "Cookie: SID=cookie-secret Authorization: Bearer token-secret"
        )
    )
    orchestrator = YouTubeEvidenceOrchestrator(
        sources=(
            YouTubeTranscriptAdapter(
                YtDlpYouTubeGateway(backend),
                source_name="yt_dlp",
            ),
        )
    )

    evidence = orchestrator.fetch(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    attempt = evidence.attempts[0]
    assert attempt.status is EvidenceAttemptStatus.FAILED
    assert attempt.code == "rate_limited"
    assert attempt.retryable is True
    assert attempt.exit_status == 1
    assert attempt.stderr_summary is not None
    assert "429" in attempt.stderr_summary
    assert "super-secret" not in attempt.stderr_summary
    assert "cookie-secret" not in attempt.stderr_summary
    assert "token-secret" not in attempt.stderr_summary
    assert "youtube.com" not in attempt.stderr_summary
    document = evidence_document(
        VideoRequest(url="https://www.youtube.com/watch?v=demo123"),
        evidence,
    )
    assert document["schema_version"] == 3
    assert document["run"]["run_id"] == evidence.run_id
    assert document["run"]["attempts"][0]["exit_status"] == 1
    assert "super-secret" not in document["run"]["attempts"][0]["stderr_summary"]


def test_user_gets_redacted_ytdlp_authentication_diagnostics() -> None:
    backend = SubprocessYtDlpBackend(
        runner=FailingCommandRunner(
            "ERROR: Sign in to confirm you're not a bot. "
            "Authorization: Bearer auth-secret https://accounts.google.com/login"
        ),
    )
    orchestrator = YouTubeEvidenceOrchestrator(
        sources=(
            YouTubeTranscriptAdapter(
                YtDlpYouTubeGateway(backend),
                source_name="yt_dlp",
            ),
        )
    )

    evidence = orchestrator.fetch(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    attempt = evidence.attempts[0]
    assert attempt.code == "authentication_required"
    assert attempt.retryable is False
    assert attempt.exit_status == 1
    assert attempt.stderr_summary is not None
    assert "auth-secret" not in attempt.stderr_summary
    assert "accounts.google.com" not in attempt.stderr_summary


def test_successful_ytdlp_metadata_parse_failure_keeps_safe_diagnostics() -> None:
    evidence = YouTubeEvidenceOrchestrator(
        sources=(
            YouTubeTranscriptAdapter(
                YtDlpYouTubeGateway(
                    SubprocessYtDlpBackend(runner=SuccessfulInvalidMetadataRunner())
                ),
                source_name="yt_dlp",
            ),
        )
    ).fetch(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    attempt = evidence.attempts[0]
    assert attempt.code == "metadata_parse_failed"
    assert attempt.exit_status == 0
    assert attempt.stderr_summary is not None
    assert "basic-secret" not in attempt.stderr_summary
    assert "cookie-secret" not in attempt.stderr_summary
    assert "camel-secret" not in attempt.stderr_summary
    assert "x-camel-secret" not in attempt.stderr_summary
    assert "auth-token-secret" not in attempt.stderr_summary
    assert "secret-key-secret" not in attempt.stderr_summary
    serialized = evidence_document(
        VideoRequest(url="https://www.youtube.com/watch?v=demo123"),
        evidence,
    )
    assert "basic-secret" not in json.dumps(serialized)


def test_successful_ytdlp_caption_parse_failure_keeps_safe_diagnostics(
    tmp_path: Path,
) -> None:
    evidence = YouTubeEvidenceOrchestrator(
        sources=(
            YouTubeTranscriptAdapter(
                YtDlpYouTubeGateway(
                    SubprocessYtDlpBackend(
                        runner=SuccessfulInvalidCaptionRunner(),
                        temporary_root=tmp_path,
                    )
                ),
                source_name="yt_dlp",
            ),
        )
    ).fetch(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    attempt = evidence.attempts[0]
    assert attempt.code == "caption_parse_failed"
    assert attempt.exit_status == 0
    assert attempt.stderr_summary is not None
    assert "caption-secret" not in attempt.stderr_summary


def test_user_sees_lightweight_site_block_before_ytdlp_fallback() -> None:
    orchestrator = YouTubeEvidenceOrchestrator(
        sources=(
            LightweightYouTubeSource(YouTubeTranscriptApiBackend(api=BlockedTranscriptApi())),
            ControlledSource("yt_dlp", _complete("yt_dlp")),
        )
    )

    evidence = orchestrator.fetch(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    assert evidence.completeness == "complete"
    assert [attempt.source for attempt in evidence.attempts] == [
        "youtube_transcript_api",
        "yt_dlp",
    ]
    assert evidence.attempts[0].code == "site_blocked"
    assert evidence.attempts[0].retryable is True


def test_current_ytdlp_failure_does_not_reuse_a_stale_caption(
    tmp_path: Path,
) -> None:
    stale_caption = tmp_path / "demo123.en.json3"
    stale_caption.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "tStartMs": 0,
                        "dDurationMs": 5_000,
                        "segs": [{"utf8": "stale transcript must not be reused"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    backend = SubprocessYtDlpBackend(
        runner=SuccessfulYtDlpWithoutCurrentCaption(),
        temporary_root=tmp_path,
    )
    orchestrator = YouTubeEvidenceOrchestrator(
        sources=(
            YouTubeTranscriptAdapter(
                YtDlpYouTubeGateway(backend),
                source_name="yt_dlp",
            ),
        )
    )

    evidence = orchestrator.fetch(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    assert evidence.completeness == "partial"
    assert evidence.segments == ()
    assert evidence.failure is not None
    assert evidence.failure.code == "caption_empty"
    assert stale_caption.read_text(encoding="utf-8").find("stale transcript") >= 0


def test_each_successful_run_has_an_independent_artifact_manifest() -> None:
    orchestrator = YouTubeEvidenceOrchestrator(
        sources=(ControlledSource("youtube_transcript_api", _complete("youtube_transcript_api")),)
    )
    request = VideoRequest(url="https://www.youtube.com/watch?v=demo123")

    first = orchestrator.fetch(request)
    second = orchestrator.fetch(request)

    assert first.run_id is not None
    assert second.run_id is not None
    assert first.run_id != second.run_id
    assert len(first.artifacts) == 1
    assert len(second.artifacts) == 1
    assert first.artifacts[0].run_id == first.run_id
    assert second.artifacts[0].run_id == second.run_id
    assert first.artifacts[0].artifact_id != second.artifacts[0].artifact_id
    assert first.artifacts[0].content_sha256 == second.artifacts[0].content_sha256
    assert first.artifacts[0].complete is True
    document = evidence_document(request, first)
    assert document["run"]["artifacts"][0]["run_id"] == first.run_id
    assert document["run"]["artifacts"][0]["kind"] == "transcript"


def test_valid_cache_hit_requires_current_track_and_content_revalidation(
    tmp_path: Path,
) -> None:
    cache = FileEvidenceCache(tmp_path / "cache")
    request = VideoRequest(url="https://www.youtube.com/watch?v=demo123")
    first = YouTubeEvidenceOrchestrator(
        sources=(ControlledSource("youtube_transcript_api", _complete("youtube_transcript_api")),),
        cache=cache,
    ).fetch(request)

    second = YouTubeEvidenceOrchestrator(
        sources=(ControlledSource("youtube_transcript_api", _complete("youtube_transcript_api")),),
        cache=cache,
    ).fetch(request)

    assert first.cache is not None
    assert first.cache.status is EvidenceCacheStatus.STORED
    assert second.cache is not None
    assert second.cache.status is EvidenceCacheStatus.HIT
    assert "current source" in second.cache.basis
    assert "content version" in second.cache.basis
    assert second.segments == first.segments
    assert [attempt.source for attempt in second.attempts] == [
        "youtube_transcript_api",
        "cache",
    ]
    assert second.attempts[-1].code == "cache_hit"
    assert second.run_id != first.run_id
    assert second.artifacts[0].run_id == second.run_id
    assert second.artifacts[0].source == "cache"


def test_changed_current_caption_invalidates_and_replaces_cache(tmp_path: Path) -> None:
    cache = FileEvidenceCache(tmp_path / "cache")
    request = VideoRequest(url="https://www.youtube.com/watch?v=demo123")
    YouTubeEvidenceOrchestrator(
        sources=(
            ControlledSource(
                "youtube_transcript_api",
                _complete("youtube_transcript_api", text="old caption version"),
            ),
        ),
        cache=cache,
    ).fetch(request)

    evidence = YouTubeEvidenceOrchestrator(
        sources=(
            ControlledSource(
                "youtube_transcript_api",
                _complete("youtube_transcript_api", text="current caption version"),
            ),
        ),
        cache=cache,
    ).fetch(request)

    assert [segment.text for segment in evidence.segments] == ["current caption version"]
    assert [attempt.source for attempt in evidence.attempts] == [
        "youtube_transcript_api",
        "cache",
    ]
    assert evidence.attempts[-1].code == "cache_changed"
    assert evidence.cache is not None
    assert evidence.cache.status is EvidenceCacheStatus.STORED


def test_locally_valid_cache_is_not_used_when_current_sources_fail(tmp_path: Path) -> None:
    cache = FileEvidenceCache(tmp_path / "cache")
    request = VideoRequest(url="https://www.youtube.com/watch?v=demo123")
    YouTubeEvidenceOrchestrator(
        sources=(
            ControlledSource(
                "youtube_transcript_api",
                _complete("youtube_transcript_api", text="previous successful transcript"),
            ),
        ),
        cache=cache,
    ).fetch(request)

    evidence = YouTubeEvidenceOrchestrator(
        sources=(
            ControlledSource(
                "youtube_transcript_api",
                _unavailable("youtube_transcript_api", "rate_limited"),
            ),
        ),
        cache=cache,
    ).fetch(request)

    assert evidence.completeness == "partial"
    assert evidence.segments == ()
    assert [attempt.source for attempt in evidence.attempts] == [
        "youtube_transcript_api",
        "cache",
    ]
    assert evidence.attempts[-1].code == "cache_unverified"
    assert evidence.cache is not None
    assert evidence.cache.status is EvidenceCacheStatus.INVALID


def test_incomplete_cache_is_rejected_before_fresh_evidence_is_used(
    tmp_path: Path,
) -> None:
    cache_directory = tmp_path / "cache"
    cache = FileEvidenceCache(cache_directory)
    request = VideoRequest(url="https://www.youtube.com/watch?v=demo123")
    YouTubeEvidenceOrchestrator(
        sources=(
            ControlledSource(
                "youtube_transcript_api",
                _complete("youtube_transcript_api", text="stale cached transcript"),
            ),
        ),
        cache=cache,
    ).fetch(request)
    cache_files = tuple(cache_directory.glob("*.json"))
    assert len(cache_files) == 1
    document = json.loads(cache_files[0].read_text(encoding="utf-8"))
    document["evidence"]["completeness"] = "partial"
    cache_files[0].write_text(json.dumps(document), encoding="utf-8")

    evidence = YouTubeEvidenceOrchestrator(
        sources=(
            ControlledSource(
                "youtube_transcript_api",
                _complete("youtube_transcript_api", text="fresh transcript"),
            ),
        ),
        cache=cache,
    ).fetch(request)

    assert [segment.text for segment in evidence.segments] == ["fresh transcript"]
    assert [attempt.source for attempt in evidence.attempts] == [
        "cache",
        "youtube_transcript_api",
    ]
    assert evidence.attempts[0].code == "cache_invalid"
    assert evidence.attempts[0].message is not None
    assert "incomplete_evidence" in evidence.attempts[0].message
    assert evidence.cache is not None
    assert evidence.cache.status is EvidenceCacheStatus.STORED


def test_current_chrome_transcript_satisfies_the_request_before_network_sources(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    capture_path = tmp_path / "current-chrome-capture.json"
    capture_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "capture_id": "capture-current-run",
                "captured_at": now.isoformat(),
                "video_id": "demo123",
                "transcript_complete": True,
                "metadata": {
                    "title": "Visible YouTube title",
                    "channel": "Visible Channel",
                    "duration_seconds": 600,
                },
                "track": {
                    "language_code": "zh-CN",
                    "is_generated": False,
                },
                "segments": [
                    {
                        "start_seconds": 5.0,
                        "end_seconds": 105.0,
                        "text": "Chrome 页面字幕片段一。",
                    },
                    {
                        "start_seconds": 105.0,
                        "end_seconds": 205.0,
                        "text": "Chrome 页面字幕片段二。",
                    },
                    {
                        "start_seconds": 205.0,
                        "end_seconds": 305.0,
                        "text": "Chrome 页面字幕片段三。",
                    },
                    {
                        "start_seconds": 305.0,
                        "end_seconds": 405.0,
                        "text": "Chrome 页面字幕片段四。",
                    },
                    {
                        "start_seconds": 405.0,
                        "end_seconds": 505.0,
                        "text": "Chrome 页面字幕片段五。",
                    },
                    {
                        "start_seconds": 505.0,
                        "end_seconds": 595.0,
                        "text": "Chrome 页面字幕片段六。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    orchestrator = YouTubeEvidenceOrchestrator(
        sources=(
            ChromeTranscriptFileSource(
                capture_path,
                expected_capture_id="capture-current-run",
                now=lambda: now,
            ),
            FailIfUsedSource(),
        )
    )

    evidence = orchestrator.fetch(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    assert evidence.completeness == "complete"
    assert evidence.transcript_source == "chrome_transcript"
    assert evidence.transcript_language == "zh-CN"
    assert [segment.text for segment in evidence.segments] == [
        "Chrome 页面字幕片段一。",
        "Chrome 页面字幕片段二。",
        "Chrome 页面字幕片段三。",
        "Chrome 页面字幕片段四。",
        "Chrome 页面字幕片段五。",
        "Chrome 页面字幕片段六。",
    ]
    assert [attempt.source for attempt in evidence.attempts] == ["chrome_transcript"]


def test_truncated_chrome_capture_cannot_stop_fresh_source_fallback(tmp_path: Path) -> None:
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    capture_path = tmp_path / "truncated-chrome-capture.json"
    capture_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "capture_id": "capture-current-run",
                "captured_at": now.isoformat(),
                "video_id": "demo123",
                "transcript_complete": True,
                "metadata": {
                    "title": "Visible YouTube title",
                    "channel": "Visible Channel",
                    "duration_seconds": 600,
                },
                "track": {
                    "language_code": "zh-CN",
                    "is_generated": False,
                },
                "segments": [
                    {
                        "start_seconds": 30.0,
                        "end_seconds": 35.0,
                        "text": "这只是一个截断片段。",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    evidence = YouTubeEvidenceOrchestrator(
        sources=(
            ChromeTranscriptFileSource(
                capture_path,
                expected_capture_id="capture-current-run",
                now=lambda: now,
            ),
            ControlledSource(
                "youtube_transcript_api",
                _complete("youtube_transcript_api", text="fresh complete transcript"),
            ),
        )
    ).fetch(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

    assert [segment.text for segment in evidence.segments] == ["fresh complete transcript"]
    assert [attempt.source for attempt in evidence.attempts] == [
        "chrome_transcript",
        "youtube_transcript_api",
    ]
    assert evidence.attempts[0].code == "chrome_capture_incomplete"


def test_chrome_capture_requires_duration_and_continuous_rows(tmp_path: Path) -> None:
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    cases: tuple[tuple[str, int | None, list[dict[str, object]]], ...] = (
        (
            "missing-duration",
            None,
            [
                {
                    "start_seconds": 30.0,
                    "end_seconds": 35.0,
                    "text": "No duration cannot prove full coverage.",
                }
            ],
        ),
        (
            "disconnected-endpoints",
            600,
            [
                {
                    "start_seconds": 0.0,
                    "end_seconds": 5.0,
                    "text": "Only the first endpoint.",
                },
                {
                    "start_seconds": 595.0,
                    "end_seconds": 600.0,
                    "text": "Only the last endpoint.",
                },
            ],
        ),
        (
            "oversized-synthetic-rows",
            600,
            [
                {
                    "start_seconds": 0.0,
                    "end_seconds": 300.0,
                    "text": "An excerpt stretched across the first half.",
                },
                {
                    "start_seconds": 300.0,
                    "end_seconds": 600.0,
                    "text": "An excerpt stretched across the second half.",
                },
            ],
        ),
    )

    for capture_id, duration, segments in cases:
        capture_path = tmp_path / f"{capture_id}.json"
        capture_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "capture_id": capture_id,
                    "captured_at": now.isoformat(),
                    "video_id": "demo123",
                    "transcript_complete": True,
                    "metadata": {
                        "title": "Visible YouTube title",
                        "channel": "Visible Channel",
                        "duration_seconds": duration,
                    },
                    "track": {
                        "language_code": "en",
                        "is_generated": False,
                    },
                    "segments": segments,
                }
            ),
            encoding="utf-8",
        )
        evidence = YouTubeEvidenceOrchestrator(
            sources=(
                ChromeTranscriptFileSource(
                    capture_path,
                    expected_capture_id=capture_id,
                    now=lambda: now,
                ),
                ControlledSource(
                    "youtube_transcript_api",
                    _complete("youtube_transcript_api", text="fresh complete transcript"),
                ),
            )
        ).fetch(VideoRequest(url="https://www.youtube.com/watch?v=demo123"))

        assert [segment.text for segment in evidence.segments] == ["fresh complete transcript"]
        assert evidence.attempts[0].code == "chrome_capture_incomplete"


def test_ytdlp_uses_the_same_manual_language_alias_selection() -> None:
    orchestrator = YouTubeEvidenceOrchestrator(
        sources=(
            YouTubeTranscriptAdapter(
                YtDlpYouTubeGateway(MultiTrackYtDlpBackend()),
                source_name="yt_dlp",
            ),
        )
    )

    evidence = orchestrator.fetch(
        VideoRequest(
            url="https://www.youtube.com/watch?v=demo123",
            preferred_languages=("zh-Hans", "zh", "en"),
        )
    )

    assert evidence.completeness == "complete"
    assert evidence.transcript_language == "zh-CN"
    assert evidence.transcript_is_generated is False


def test_ytdlp_declared_original_language_beats_track_order() -> None:
    evidence = YouTubeEvidenceOrchestrator(
        sources=(
            YouTubeTranscriptAdapter(
                YtDlpYouTubeGateway(DeclaredOriginalYtDlpBackend()),
                source_name="yt_dlp",
            ),
        )
    ).fetch(
        VideoRequest(
            url="https://www.youtube.com/watch?v=demo123",
            preferred_languages=("fr",),
        )
    )

    assert evidence.transcript_language == "ja"
    assert [segment.text for segment in evidence.segments] == ["first yt-dlp manual track"]


def test_requested_generated_language_beats_an_unrelated_manual_track() -> None:
    evidence = YouTubeEvidenceOrchestrator(
        sources=(LightweightYouTubeSource(PreferredGeneratedBackend()),)
    ).fetch(
        VideoRequest(
            url="https://www.youtube.com/watch?v=demo123",
            preferred_languages=("en",),
        )
    )

    assert evidence.completeness == "complete"
    assert evidence.transcript_language == "en"
    assert evidence.transcript_is_generated is True


def test_lightweight_uses_verified_default_caption_language() -> None:
    evidence = YouTubeEvidenceOrchestrator(
        sources=(
            LightweightYouTubeSource(
                YouTubeTranscriptApiBackend(api=ManualTranscriptApi()),
                metadata_provider=ControlledJapaneseOriginalMetadata(),
            ),
        )
    ).fetch(
        VideoRequest(
            url="https://www.youtube.com/watch?v=demo123",
            preferred_languages=("fr",),
        )
    )

    assert evidence.transcript_language == "ja"
    assert [segment.text for segment in evidence.segments] == ["verified original track"]


def test_lightweight_success_keeps_compact_public_metadata() -> None:
    evidence = YouTubeEvidenceOrchestrator(
        sources=(
            LightweightYouTubeSource(
                ControlledLightweightBackend(),
                metadata_provider=ControlledLightweightMetadata(),
            ),
        )
    ).fetch(
        VideoRequest(
            url="https://www.youtube.com/watch?v=demo123",
            preferred_languages=("zh-Hans",),
        )
    )

    assert evidence.completeness == "complete"
    assert evidence.metadata.title == "Compact public title"
    assert evidence.metadata.channel == "Compact public channel"
    assert evidence.metadata.duration_seconds == 600


def test_public_metadata_provider_keeps_oembed_identity_and_watch_duration(
    monkeypatch: MonkeyPatch,
) -> None:
    def controlled_urlopen(
        request: Request,
        timeout: float,
    ) -> ControlledHttpResponse:
        assert timeout == 10.0
        if "oembed" in request.full_url:
            return ControlledHttpResponse(
                json.dumps(
                    {
                        "title": "Public title",
                        "author_name": "Public channel",
                    }
                ).encode("utf-8")
            )
        assert request.full_url == "https://www.youtube.com/watch?v=demo123"
        player = {
            "videoDetails": {"lengthSeconds": "601"},
            "captions": {
                "playerCaptionsTracklistRenderer": {
                    "captionTracks": [
                        {"languageCode": "en"},
                        {"languageCode": "ja"},
                    ],
                    "audioTracks": [
                        {
                            "hasDefaultTrack": True,
                            "defaultCaptionTrackIndex": 1,
                        }
                    ],
                }
            },
        }
        page = f"<script>var ytInitialPlayerResponse = {json.dumps(player)};</script>"
        return ControlledHttpResponse(page.encode("utf-8"))

    monkeypatch.setattr("video_digest.youtube_sources.urlopen", controlled_urlopen)

    metadata = YouTubeOEmbedMetadataProvider().fetch("demo123")

    assert metadata.title == "Public title"
    assert metadata.channel == "Public channel"
    assert metadata.duration_seconds == 601

    provider = YouTubeOEmbedMetadataProvider()
    assert provider.original_caption_language("demo123") == "ja"
    assert provider.fetch("demo123").duration_seconds == 601
