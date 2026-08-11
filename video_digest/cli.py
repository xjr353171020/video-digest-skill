from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .domain import VideoRequest
from .serialization import evidence_document, failed_request_document
from .youtube import SubprocessYtDlpBackend, YouTubeTranscriptAdapter, YtDlpYouTubeGateway


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    languages = tuple(arguments.language or VideoRequest.preferred_languages)
    request = VideoRequest(
        url=arguments.url,
        focus=arguments.focus,
        preferred_languages=languages,
    )
    adapter = YouTubeTranscriptAdapter(
        YtDlpYouTubeGateway(
            SubprocessYtDlpBackend(timeout_seconds=arguments.timeout_seconds),
        )
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
        description="Fetch a compact, timestamped evidence bundle from one public YouTube video.",
    )
    parser.add_argument("url", help="Public YouTube watch or youtu.be URL")
    parser.add_argument("--focus", help="Question or topic the later digest should prioritize")
    parser.add_argument(
        "--language",
        action="append",
        help="Preferred caption language in priority order; repeat for multiple languages",
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


def _write_document(document: dict[str, Any], output: Path | None) -> None:
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        print(rendered, end="")
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(str(output.resolve()))
