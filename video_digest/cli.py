from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .cache import FileEvidenceCache
from .chrome_source import ChromeTranscriptFileSource
from .domain import VideoRequest
from .orchestration import YouTubeEvidenceOrchestrator
from .serialization import evidence_document, failed_request_document
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
    adapter = YouTubeEvidenceOrchestrator(
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
        cache=cache,
    )
    exit_code = 0
    try:
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
        description="Fetch a compact, timestamped evidence bundle from one public YouTube video or Short.",
    )
    parser.add_argument("url", help="Public YouTube watch, Shorts, or youtu.be URL")
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
    return parser


def _default_cache_directory() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path(tempfile.gettempdir())
    return root / "video-digest-skill" / "cache"


def _write_document(document: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        print(rendered, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(str(output.resolve()))
