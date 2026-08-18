from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

from video_digest import (
    BilibiliChromeTranscriptFileSource,
    BilibiliYtDlpSource,
    DigestRecommendation,
    DigestResult,
    DigestRunStatus,
    DigestWorkflow,
    EvidenceAttemptStatus,
    EvidenceCacheStatus,
    EvidenceOrchestrator,
    FileEvidenceCache,
    SubprocessBilibiliBackend,
    VideoRequest,
    WatchSegment,
)
from video_digest.cli import main
from video_digest.serialization import evidence_document
from video_digest.video_urls import bilibili_video_reference

_BILIBILI_URL = "https://www.bilibili.com/video/BV1X7411F744?p=5"


@pytest.mark.parametrize(
    ("url", "page"),
    (
        ("https://www.bilibili.com/video/BV1X7411F744", 1),
        ("https://www.bilibili.com/video/BV1X7411F744?p=5", 5),
        ("https://m.bilibili.com/video/BV1X7411F744/?p=5&spm_id_from=333", 5),
    ),
)
def test_bilibili_video_reference_preserves_part_identity(url: str, page: int) -> None:
    reference = bilibili_video_reference(url)

    assert reference.platform == "bilibili"
    assert reference.bvid == "BV1X7411F744"
    assert reference.page == page
    assert reference.video_id == f"BV1X7411F744_p{page}"
    assert reference.canonical_url == (f"https://www.bilibili.com/video/BV1X7411F744?p={page}")


def test_current_bilibili_chrome_capture_enters_the_unified_evidence_contract(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    capture_path = _write_browser_capture(tmp_path, now=now)
    output_path = tmp_path / "evidence.json"

    exit_code = main(
        [
            _BILIBILI_URL,
            "--chrome-transcript",
            str(capture_path),
            "--chrome-capture-id",
            "bilibili-current-run",
            "--no-cache",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    document = json.loads(output_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 3
    assert document["status"] == "complete"
    assert document["evidence"]["metadata"] == {
        "platform": "bilibili",
        "video_id": "BV1X7411F744_p5",
        "title": "Lecture 05 Rasterization 1 (Triangles)",
        "channel": "GAMES-Webinar",
        "duration_seconds": 120,
        "canonical_url": _BILIBILI_URL,
    }
    assert document["evidence"]["transcript_source"] == "bilibili_browser_transcript"
    assert document["evidence"]["transcript_language"] == "zh"
    assert document["evidence"]["transcript_is_generated"] is True
    assert [attempt["source"] for attempt in document["run"]["attempts"]] == [
        "bilibili_browser_transcript"
    ]
    assert document["evidence"]["media_downloaded"] is False


def test_bilibili_output_uses_the_canonical_url_without_tracking_parameters(
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    capture_path = _write_browser_capture(tmp_path, now=now)
    output_path = tmp_path / "evidence.json"
    tracked_url = f"{_BILIBILI_URL}&vd_source=private-tracking-value"

    exit_code = main(
        [
            tracked_url,
            "--chrome-transcript",
            str(capture_path),
            "--chrome-capture-id",
            "bilibili-current-run",
            "--no-cache",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    serialized = output_path.read_text(encoding="utf-8")
    document = json.loads(serialized)
    assert document["request"]["url"] == _BILIBILI_URL
    assert document["evidence"]["metadata"]["canonical_url"] == _BILIBILI_URL
    assert "private-tracking-value" not in serialized


def test_bilibili_browser_capture_allows_real_subtitle_gaps(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    source = BilibiliChromeTranscriptFileSource(
        _write_browser_capture(tmp_path, now=now),
        expected_capture_id="bilibili-current-run",
        now=lambda: now,
    )

    evidence = source.fetch(VideoRequest(url=_BILIBILI_URL))

    assert evidence.completeness == "complete"
    assert evidence.failure is None
    assert [segment.text for segment in evidence.segments] == [
        "第一段中文字幕。",
        "字幕中间允许有真实的静音间隔。",
        "最后一段中文字幕。",
    ]


@pytest.mark.parametrize(
    "sensitive_field",
    ("cookie", "authorization", "token", "signed_url", "subtitle_url"),
)
def test_bilibili_browser_capture_rejects_sensitive_fields(
    tmp_path: Path,
    sensitive_field: str,
) -> None:
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    capture_path = _write_browser_capture(tmp_path, now=now)
    document = json.loads(capture_path.read_text(encoding="utf-8"))
    document["track"][sensitive_field] = "must-not-enter-evidence"
    capture_path.write_text(json.dumps(document), encoding="utf-8")
    source = BilibiliChromeTranscriptFileSource(
        capture_path,
        expected_capture_id="bilibili-current-run",
        now=lambda: now,
    )

    evidence = source.fetch(VideoRequest(url=_BILIBILI_URL))

    assert evidence.completeness == "partial"
    assert evidence.segments == ()
    assert evidence.failure is not None
    assert evidence.failure.code == "chrome_capture_invalid"
    assert evidence.failure.message.endswith("sensitive_field_rejected.")
    assert "must-not-enter-evidence" not in json.dumps(
        evidence_document(VideoRequest(url=_BILIBILI_URL), evidence),
        ensure_ascii=False,
    )


def test_local_bilibili_adapter_selects_one_manual_chinese_track(
    tmp_path: Path,
) -> None:
    runner = SuccessfulBilibiliRunner()
    evidence = EvidenceOrchestrator(
        sources=(
            BilibiliYtDlpSource(SubprocessBilibiliBackend(runner=runner, temporary_root=tmp_path)),
        )
    ).fetch(VideoRequest(url=_BILIBILI_URL))

    assert evidence.completeness == "complete"
    assert evidence.metadata.platform == "bilibili"
    assert evidence.metadata.video_id == "BV1X7411F744_p5"
    assert evidence.transcript_source == "bilibili_yt_dlp"
    assert evidence.transcript_language == "zh-CN"
    assert evidence.transcript_is_generated is False
    assert [segment.text for segment in evidence.segments] == [
        "人工中文字幕第一段。",
        "人工中文字幕第二段。",
    ]
    assert evidence.media_downloaded is False
    assert len(runner.commands) == 3
    caption_command = runner.commands[-1]
    assert caption_command[caption_command.index("--sub-langs") + 1] == "zh-CN"
    assert all(".*" not in argument for argument in caption_command)


@pytest.mark.parametrize(
    ("runner_kind", "code", "status"),
    (
        (
            "authentication",
            "authentication_required",
            EvidenceAttemptStatus.FAILED,
        ),
        (
            "no_caption",
            "captions_unavailable",
            EvidenceAttemptStatus.UNAVAILABLE,
        ),
        (
            "site_blocked",
            "site_blocked",
            EvidenceAttemptStatus.FAILED,
        ),
        (
            "temporary_failure",
            "temporary_failure",
            EvidenceAttemptStatus.FAILED,
        ),
    ),
)
def test_local_bilibili_failures_remain_distinct(
    runner_kind: str,
    code: str,
    status: EvidenceAttemptStatus,
) -> None:
    runners: dict[str, BilibiliRunner] = {
        "authentication": AuthenticationRequiredRunner(),
        "no_caption": NoCaptionRunner(),
        "site_blocked": SiteBlockedRunner(),
        "temporary_failure": TemporaryFailureRunner(),
    }
    runner = runners[runner_kind]
    evidence = EvidenceOrchestrator(
        sources=(BilibiliYtDlpSource(SubprocessBilibiliBackend(runner=runner)),)
    ).fetch(VideoRequest(url=_BILIBILI_URL))

    assert evidence.completeness == "partial"
    assert evidence.segments == ()
    assert evidence.failure is not None
    assert evidence.failure.code == code
    assert evidence.attempts[0].status is status


def test_cookie_database_lock_is_reported_without_leaking_or_copying(
    tmp_path: Path,
) -> None:
    runner = CookieDatabaseLockedRunner()
    evidence = EvidenceOrchestrator(
        sources=(
            BilibiliYtDlpSource(
                SubprocessBilibiliBackend(
                    runner=runner,
                    temporary_root=tmp_path,
                    cookies_from_browser="chrome",
                )
            ),
        )
    ).fetch(VideoRequest(url=_BILIBILI_URL))

    assert evidence.failure is not None
    assert evidence.failure.code == "cookie_database_locked"
    assert evidence.failure.stderr_summary is not None
    serialized = json.dumps(
        evidence_document(VideoRequest(url=_BILIBILI_URL), evidence),
        ensure_ascii=False,
    )
    assert "PrivateProfile" not in serialized
    assert "private-cookie-value" not in serialized
    assert "Network\\Cookies" not in serialized
    assert len(runner.commands) == 1
    assert "--cookies-from-browser" in runner.commands[0]
    assert not tuple(tmp_path.iterdir())


def test_bilibili_cache_rejects_platform_mismatch_even_when_fingerprint_matches(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    cache_directory = tmp_path / "cache"
    cache = FileEvidenceCache(cache_directory, now=lambda: now)
    request = VideoRequest(url=_BILIBILI_URL)
    current = BilibiliChromeTranscriptFileSource(
        _write_browser_capture(tmp_path, now=now),
        expected_capture_id="bilibili-current-run",
        now=lambda: now,
    ).fetch(request)
    stored = cache.store(request, current)
    assert stored.status is EvidenceCacheStatus.STORED
    cache_files = tuple(cache_directory.glob("*.json"))
    assert len(cache_files) == 1
    document = json.loads(cache_files[0].read_text(encoding="utf-8"))
    document["evidence"]["metadata"]["platform"] = "youtube"
    cache_files[0].write_text(json.dumps(document), encoding="utf-8")

    lookup = cache.lookup(request)

    assert lookup.info.status is EvidenceCacheStatus.INVALID
    assert lookup.evidence is None
    assert "platform" in lookup.info.basis


def test_bilibili_evidence_uses_the_same_digest_workflow(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    adapter = EvidenceOrchestrator(
        sources=(
            BilibiliChromeTranscriptFileSource(
                _write_browser_capture(tmp_path, now=now),
                expected_capture_id="bilibili-current-run",
                now=lambda: now,
            ),
        )
    )

    run = DigestWorkflow(adapter, ControlledBilibiliSummarizer()).run(
        VideoRequest(url=_BILIBILI_URL)
    )

    assert run.status is DigestRunStatus.COMPLETED
    assert run.digest is not None
    assert run.digest.recommendation is DigestRecommendation.WATCH_SELECTED
    assert run.evidence is not None
    assert run.evidence.metadata.platform == "bilibili"


def _write_browser_capture(
    directory: Path,
    *,
    now: datetime,
) -> Path:
    capture_path = directory / "bilibili-browser-capture.json"
    capture_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "platform": "bilibili",
                "capture_id": "bilibili-current-run",
                "captured_at": now.isoformat(),
                "bvid": "BV1X7411F744",
                "page": 5,
                "cid": "156365564",
                "transcript_complete": True,
                "metadata": {
                    "title": "Lecture 05 Rasterization 1 (Triangles)",
                    "channel": "GAMES-Webinar",
                    "duration_seconds": 120,
                },
                "track": {
                    "language_code": "zh",
                    "is_generated": True,
                    "label": "中文",
                },
                "segments": [
                    {
                        "start_seconds": 1.0,
                        "end_seconds": 5.0,
                        "text": "第一段中文字幕。",
                    },
                    {
                        "start_seconds": 30.0,
                        "end_seconds": 35.0,
                        "text": "字幕中间允许有真实的静音间隔。",
                    },
                    {
                        "start_seconds": 110.0,
                        "end_seconds": 119.0,
                        "text": "最后一段中文字幕。",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return capture_path


class BilibiliRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(
        self,
        command: list[str],
        timeout_seconds: float,
    ) -> CompletedProcess[str]:
        raise NotImplementedError


class SuccessfulBilibiliRunner(BilibiliRunner):
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
    ) -> CompletedProcess[str]:
        self.commands.append(command)
        assert timeout_seconds == 90.0
        if "--list-subs" in command:
            return CompletedProcess(
                command,
                0,
                stdout=(
                    "[info] Available subtitles for BV1X7411F744_p5:\n"
                    "Language Formats\n"
                    "ai-zh    srt\n"
                    "zh-CN    srt\n"
                    "danmaku  xml\n"
                ),
                stderr="",
            )
        if "--dump-single-json" in command:
            return CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "id": "BV1X7411F744_p5",
                        "title": "Lecture 05 Rasterization 1 (Triangles)",
                        "uploader": "GAMES-Webinar",
                        "duration": 120,
                    }
                ),
                stderr="",
            )
        assert "--write-subs" in command
        output_template = Path(command[command.index("--output") + 1])
        output_template.parent.joinpath("BV1X7411F744_p5.zh-CN.srt").write_text(
            "1\n"
            "00:00:01,000 --> 00:00:05,000\n"
            "人工中文字幕第一段。\n\n"
            "2\n"
            "00:01:50,000 --> 00:01:59,000\n"
            "人工中文字幕第二段。\n",
            encoding="utf-8",
        )
        return CompletedProcess(command, 0, stdout="", stderr="")


class AuthenticationRequiredRunner(BilibiliRunner):
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
    ) -> CompletedProcess[str]:
        self.commands.append(command)
        return CompletedProcess(
            command,
            0,
            stdout=(
                "[info] Available subtitles for BV1X7411F744_p5:\nLanguage Formats\ndanmaku xml\n"
            ),
            stderr=(
                "WARNING: Subtitles are only available when logged in. "
                "Use --cookies-from-browser for authentication."
            ),
        )


class NoCaptionRunner(BilibiliRunner):
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
    ) -> CompletedProcess[str]:
        self.commands.append(command)
        return CompletedProcess(
            command,
            0,
            stdout=(
                "[info] Available subtitles for BV1X7411F744_p5:\nLanguage Formats\ndanmaku xml\n"
            ),
            stderr="",
        )


class SiteBlockedRunner(BilibiliRunner):
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
    ) -> CompletedProcess[str]:
        self.commands.append(command)
        return CompletedProcess(
            command,
            1,
            stdout="",
            stderr="ERROR: HTTP Error 412 from https://api.bilibili.com/x/player/wbi/v2",
        )


class CookieDatabaseLockedRunner(BilibiliRunner):
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
    ) -> CompletedProcess[str]:
        self.commands.append(command)
        return CompletedProcess(
            command,
            1,
            stdout="",
            stderr=(
                "ERROR: Could not copy Chrome cookie database: Permission denied: "
                "C:\\Users\\PrivateProfile\\AppData\\Local\\Google\\Chrome\\User Data\\"
                "Default\\Network\\Cookies Cookie: private-cookie-value"
            ),
        )


class TemporaryFailureRunner(BilibiliRunner):
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
    ) -> CompletedProcess[str]:
        self.commands.append(command)
        return CompletedProcess(
            command,
            1,
            stdout="",
            stderr="ERROR: HTTP Error 503: Service Temporarily Unavailable",
        )


class ControlledBilibiliSummarizer:
    def summarize(self, request: VideoRequest, evidence: Any) -> DigestResult:
        assert request.url == _BILIBILI_URL
        assert evidence.metadata.platform == "bilibili"
        return DigestResult(
            one_sentence_conclusion="这是统一摘要流程。",
            core_points=("第一点", "第二点"),
            watch_segments=(
                WatchSegment(
                    start_seconds=30.0,
                    end_seconds=35.0,
                    reason="关键片段",
                ),
            ),
            information_density=8,
            worth_watching=7,
            recommendation=DigestRecommendation.WATCH_SELECTED,
            estimated_minutes_saved=1,
        )
