from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .asr import AsrModelNotice, FasterWhisperTranscriber, LocalAsrSource, YtDlpAudioBackend
from .bilibili import (
    BilibiliChromeTranscriptFileSource,
    BilibiliYtDlpSource,
    SubprocessBilibiliBackend,
)
from .cache import FileEvidenceCache
from .chrome_source import ChromeTranscriptFileSource
from .domain import VideoRequest
from .orchestration import EvidenceOrchestrator
from .serialization import evidence_document, failed_request_document
from .video_urls import video_reference
from .youtube import SubprocessYtDlpBackend, YouTubeTranscriptAdapter, YtDlpYouTubeGateway
from .youtube_sources import (
    LightweightYouTubeSource,
    YouTubeOEmbedMetadataProvider,
    YouTubeTranscriptApiBackend,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if (arguments.chrome_transcript is None) != (arguments.chrome_capture_id is None):
        parser.error("--chrome-transcript and --chrome-capture-id must be provided together")
    languages = tuple(arguments.language or VideoRequest.preferred_languages)
    request = VideoRequest(
        url=arguments.url,
        focus=arguments.focus,
        preferred_languages=languages,
    )
    cache = None
    if not arguments.no_cache:
        cache = FileEvidenceCache(arguments.cache_directory or _default_cache_directory())
    exit_code = 0
    try:
        reference = video_reference(request.url)
        asr_fallback = _asr_fallback(arguments, platform=reference.platform)
        if reference.platform == "youtube":
            adapter = EvidenceOrchestrator(
                sources=(
                    ChromeTranscriptFileSource(
                        arguments.chrome_transcript,
                        expected_capture_id=arguments.chrome_capture_id,
                    ),
                    LightweightYouTubeSource(
                        YouTubeTranscriptApiBackend(),
                        metadata_provider=YouTubeOEmbedMetadataProvider(),
                    ),
                    YouTubeTranscriptAdapter(
                        YtDlpYouTubeGateway(
                            SubprocessYtDlpBackend(timeout_seconds=arguments.timeout_seconds),
                        ),
                        source_name="yt_dlp",
                    ),
                ),
                fallback_source=asr_fallback,
                cache=cache,
            )
        else:
            adapter = EvidenceOrchestrator(
                sources=(
                    BilibiliChromeTranscriptFileSource(
                        arguments.chrome_transcript,
                        expected_capture_id=arguments.chrome_capture_id,
                    ),
                    BilibiliYtDlpSource(
                        SubprocessBilibiliBackend(
                            timeout_seconds=arguments.timeout_seconds,
                            cookies_from_browser=arguments.bilibili_cookies_from_browser,
                        )
                    ),
                ),
                fallback_source=asr_fallback,
                cache=cache,
            )
        evidence = adapter.fetch(request)
    except ValueError as error:
        document = failed_request_document(request, str(error))
        exit_code = 2
    else:
        document = evidence_document(request, evidence)
    _write_document(document, arguments.output)
    return exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-digest",
        description=(
            "Fetch a compact, timestamped evidence bundle from one public YouTube or "
            "Bilibili video."
        ),
    )
    parser.add_argument("url", help="Public YouTube, YouTube Shorts, youtu.be, or Bilibili URL")
    parser.add_argument("--focus", help="Question or topic the later digest should prioritize")
    parser.add_argument(
        "--language",
        action="append",
        help="Preferred caption language in priority order; repeat for multiple languages",
    )
    parser.add_argument(
        "--chrome-transcript",
        type=Path,
        help="Current-run Chrome transcript capture JSON created by the skill",
    )
    parser.add_argument(
        "--chrome-capture-id",
        help="Capture ID embedded in --chrome-transcript for current-run validation",
    )
    parser.add_argument(
        "--cache-directory",
        type=Path,
        help="Directory for validated complete transcript evidence cache",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable local evidence cache lookup and storage for this run",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write UTF-8 JSON to this file instead of stdout",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=90.0,
        help="Per yt-dlp operation timeout (default: 90)",
    )
    parser.add_argument(
        "--bilibili-cookies-from-browser",
        help=(
            "Explicit yt-dlp browser selector for Bilibili subtitles, such as chrome. "
            "Prefer a current connected-browser capture; a locked Cookie database is not copied."
        ),
    )
    parser.add_argument(
        "--disable-asr",
        action="store_true",
        help="Do not use local speech-to-text after all caption sources are unavailable",
    )
    parser.add_argument(
        "--asr-model",
        default="small",
        choices=("tiny", "base", "small", "medium", "turbo", "large-v3"),
        help="Local faster-whisper model (default: small)",
    )
    parser.add_argument(
        "--asr-device",
        default="cpu",
        choices=("cpu", "cuda"),
        help="Local ASR execution device (default: cpu)",
    )
    parser.add_argument(
        "--asr-compute-type",
        choices=("auto", "int8", "int8_float16", "int8_float32", "float16", "float32"),
        help="CTranslate2 compute type (default: int8 on CPU, float16 on CUDA)",
    )
    parser.add_argument(
        "--asr-model-directory",
        type=Path,
        help="Directory containing or receiving the local faster-whisper model",
    )
    parser.add_argument(
        "--asr-temporary-directory",
        type=Path,
        help="Directory for current-run audio-only ASR artifacts",
    )
    parser.add_argument(
        "--allow-asr-model-download",
        action="store_true",
        help=(
            "Authorize a first local faster-whisper model download after the cost notice is "
            "written to stderr"
        ),
    )
    parser.add_argument(
        "--keep-asr-audio",
        action="store_true",
        help="Retain this run's temporary audio for debugging instead of deleting it",
    )
    return parser


def _default_cache_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path(tempfile.gettempdir())
    return root / "video-digest-skill" / "cache"


def _default_asr_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path(tempfile.gettempdir())
    return root / "video-digest-skill" / "asr"


def _asr_fallback(
    arguments: argparse.Namespace,
    *,
    platform: str,
) -> LocalAsrSource | None:
    if arguments.disable_asr:
        return None
    root = _default_asr_root()
    model_directory = arguments.asr_model_directory or root / "models" / arguments.asr_model
    temporary_root = arguments.asr_temporary_directory or root / "temporary-audio"
    browser_selector = (
        arguments.bilibili_cookies_from_browser if platform == "bilibili" else None
    )
    compute_type = arguments.asr_compute_type or (
        "float16" if arguments.asr_device == "cuda" else "int8"
    )
    return LocalAsrSource(
        audio_backend=YtDlpAudioBackend(
            timeout_seconds=max(arguments.timeout_seconds, 600.0),
            cookies_from_browser=browser_selector,
        ),
        transcriber=FasterWhisperTranscriber(
            model_name=arguments.asr_model,
            model_directory=model_directory,
            device=arguments.asr_device,
            compute_type=compute_type,
        ),
        model_directory=model_directory,
        temporary_root=temporary_root,
        allow_model_download=arguments.allow_asr_model_download,
        keep_audio=arguments.keep_asr_audio,
        model_notice=_write_model_notice if arguments.allow_asr_model_download else None,
    )


def _write_model_notice(notice: AsrModelNotice) -> None:
    size_mib = (notice.estimated_download_bytes + 1024 * 1024 - 1) // (1024 * 1024)
    print(
        (
            f"Local ASR model download authorized: {notice.model_name}, about {size_mib:,} MiB; "
            f"device={notice.device}, compute_type={notice.compute_type}. The model remains local."
        ),
        file=sys.stderr,
    )


def _write_document(document: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        print(rendered, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(str(output.resolve()))
