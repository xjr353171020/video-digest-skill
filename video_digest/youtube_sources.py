from __future__ import annotations

import html
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .caption_selection import choose_caption_track
from .domain import (
    CaptionTrack,
    DigestFailure,
    EvidenceBundle,
    TranscriptSegment,
    VideoMetadata,
    VideoRequest,
)
from .youtube import YouTubeGatewayFailure, youtube_video_id


class LightweightCaptionBackend(Protocol):
    def list_tracks(self, video_id: str) -> tuple[CaptionTrack, ...]: ...

    def fetch_caption(
        self,
        video_id: str,
        track: CaptionTrack,
    ) -> tuple[TranscriptSegment, ...]: ...


class LightweightMetadataProvider(Protocol):
    def fetch(self, video_id: str) -> VideoMetadata: ...

    def original_caption_language(self, video_id: str) -> str | None: ...


class TranscriptApiSnippet(Protocol):
    text: str
    start: float
    duration: float


class TranscriptApiTrack(Protocol):
    language: str
    language_code: str
    is_generated: bool

    def fetch(self) -> Iterable[TranscriptApiSnippet]: ...


class TranscriptApiClient(Protocol):
    def list(self, video_id: str) -> Iterable[TranscriptApiTrack]: ...


class YouTubeTranscriptApiBackend:
    def __init__(self, *, api: TranscriptApiClient | None = None) -> None:
        self._api = api
        self._listed_tracks: dict[str, TranscriptApiTrack] = {}

    def list_tracks(self, video_id: str) -> tuple[CaptionTrack, ...]:
        try:
            native_tracks = tuple(self._client().list(video_id))
        except YouTubeGatewayFailure:
            raise
        except Exception as error:
            raise _classify_transcript_api_failure("subtitles", error) from error

        tracks: list[CaptionTrack] = []
        self._listed_tracks.clear()
        for index, native_track in enumerate(native_tracks):
            identifier = (
                f"{video_id}:{native_track.language_code}:"
                f"{'auto' if native_track.is_generated else 'manual'}:{index}"
            )
            self._listed_tracks[identifier] = native_track
            tracks.append(
                CaptionTrack(
                    identifier=identifier,
                    language_code=native_track.language_code,
                    display_name=native_track.language,
                    is_generated=native_track.is_generated,
                    is_original=False,
                )
            )
        return tuple(tracks)

    def fetch_caption(
        self,
        video_id: str,
        track: CaptionTrack,
    ) -> tuple[TranscriptSegment, ...]:
        native_track = self._listed_tracks.get(track.identifier)
        if native_track is None or not track.identifier.startswith(f"{video_id}:"):
            raise YouTubeGatewayFailure(
                stage="subtitles",
                code="caption_track_stale",
                message="The selected lightweight caption track is not part of this run.",
                retryable=True,
            )
        try:
            snippets = tuple(native_track.fetch())
        except Exception as error:
            raise _classify_transcript_api_failure("subtitles", error) from error

        segments: list[TranscriptSegment] = []
        has_invalid_timing = False
        for snippet in snippets:
            text = re.sub(r"\s+", " ", html.unescape(str(snippet.text))).strip()
            if not text:
                continue
            try:
                start = float(snippet.start)
                duration = float(snippet.duration)
            except (TypeError, ValueError):
                has_invalid_timing = True
                continue
            if (
                not math.isfinite(start)
                or not math.isfinite(duration)
                or start < 0
                or duration <= 0
            ):
                has_invalid_timing = True
                continue
            segments.append(
                TranscriptSegment(
                    start_seconds=start,
                    end_seconds=start + duration,
                    text=text,
                )
            )
        if has_invalid_timing:
            raise YouTubeGatewayFailure(
                stage="subtitles",
                code="caption_timestamps_missing",
                message="Some caption text did not include reliable start and end times.",
                retryable=False,
            )
        return tuple(segments)

    def _client(self) -> TranscriptApiClient:
        if self._api is not None:
            return self._api
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ModuleNotFoundError as error:
            raise YouTubeGatewayFailure(
                stage="subtitles",
                code="dependency_missing",
                message=(
                    "youtube-transcript-api is unavailable. Run uv sync in the skill "
                    "directory and retry."
                ),
                retryable=False,
            ) from error
        self._api = cast(TranscriptApiClient, YouTubeTranscriptApi())
        return self._api


class YouTubeOEmbedMetadataProvider:
    def __init__(self, *, timeout_seconds: float = 10.0) -> None:
        self._timeout_seconds = timeout_seconds
        self._watch_contexts: dict[str, _WatchContext] = {}

    def fetch(self, video_id: str) -> VideoMetadata:
        fallback = _placeholder_metadata(video_id)
        title = fallback.title
        channel = fallback.channel
        query = urlencode(
            {
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "format": "json",
            }
        )
        request = Request(
            f"https://www.youtube.com/oembed?{query}",
            headers={"User-Agent": "video-digest-skill/0.2"},
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read(_MAX_OEMBED_BYTES + 1)
            if len(raw) <= _MAX_OEMBED_BYTES:
                value = json.loads(raw.decode("utf-8"))
                if isinstance(value, dict):
                    title_value = value.get("title")
                    channel_value = value.get("author_name")
                    if isinstance(title_value, str) and title_value.strip():
                        title = title_value.strip()
                    if isinstance(channel_value, str) and channel_value.strip():
                        channel = channel_value.strip()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        duration = self._watch_context(video_id).duration_seconds
        return VideoMetadata(
            video_id=video_id,
            title=title,
            channel=channel,
            duration_seconds=duration,
            canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        )

    def original_caption_language(self, video_id: str) -> str | None:
        return self._watch_context(video_id).original_caption_language

    def _watch_context(self, video_id: str) -> _WatchContext:
        cached = self._watch_contexts.get(video_id)
        if cached is not None:
            return cached
        request = Request(
            f"https://www.youtube.com/watch?v={video_id}",
            headers={
                "Accept-Language": "en-US,en;q=0.8",
                "User-Agent": "video-digest-skill/0.2",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read(_MAX_WATCH_BYTES + 1)
            if len(raw) > _MAX_WATCH_BYTES:
                context = _WatchContext(None, None)
                self._watch_contexts[video_id] = context
                return context
            page = raw.decode("utf-8", errors="replace")
        except OSError:
            context = _WatchContext(None, None)
            self._watch_contexts[video_id] = context
            return context
        context = _watch_context_from_page(page)
        self._watch_contexts[video_id] = context
        return context


class LightweightYouTubeSource:
    name = "youtube_transcript_api"

    def __init__(
        self,
        backend: LightweightCaptionBackend,
        *,
        metadata_provider: LightweightMetadataProvider | None = None,
    ) -> None:
        self._backend = backend
        self._metadata_provider = metadata_provider

    def fetch(self, request: VideoRequest) -> EvidenceBundle:
        video_id = youtube_video_id(request.url)
        metadata = _placeholder_metadata(video_id)
        try:
            tracks = self._backend.list_tracks(video_id)
        except YouTubeGatewayFailure as failure:
            return _failed_lightweight_evidence(metadata, failure)
        if not tracks:
            return EvidenceBundle(
                metadata=metadata,
                segments=(),
                transcript_source=self.name,
                transcript_language=None,
                transcript_is_generated=False,
                completeness="partial",
                media_downloaded=False,
                failure=DigestFailure(
                    stage="subtitles",
                    code="captions_unavailable",
                    message="The video does not expose a usable caption track.",
                    retryable=False,
                ),
            )
        if self._metadata_provider is not None:
            original_language = self._metadata_provider.original_caption_language(video_id)
            if original_language is not None:
                tracks = tuple(
                    replace(
                        track,
                        is_original=_same_language(track.language_code, original_language),
                    )
                    for track in tracks
                )
        track = choose_caption_track(tracks, request.preferred_languages)
        try:
            segments = self._backend.fetch_caption(video_id, track)
        except YouTubeGatewayFailure as failure:
            return _failed_lightweight_evidence(
                metadata,
                failure,
                track=track,
            )
        if not segments:
            return EvidenceBundle(
                metadata=metadata,
                segments=(),
                transcript_source=self.name,
                transcript_language=track.language_code,
                transcript_is_generated=track.is_generated,
                completeness="partial",
                media_downloaded=False,
                failure=DigestFailure(
                    stage="subtitles",
                    code="caption_empty",
                    message="The selected caption track did not contain usable text.",
                    retryable=False,
                ),
            )
        if self._metadata_provider is not None:
            metadata = self._metadata_provider.fetch(video_id)
        return EvidenceBundle(
            metadata=metadata,
            segments=segments,
            transcript_source=self.name,
            transcript_language=track.language_code,
            transcript_is_generated=track.is_generated,
            completeness="complete",
            media_downloaded=False,
        )


def _failed_lightweight_evidence(
    metadata: VideoMetadata,
    failure: YouTubeGatewayFailure,
    *,
    track: CaptionTrack | None = None,
) -> EvidenceBundle:
    return EvidenceBundle(
        metadata=metadata,
        segments=(),
        transcript_source="youtube_transcript_api",
        transcript_language=track.language_code if track is not None else None,
        transcript_is_generated=track.is_generated if track is not None else False,
        completeness="partial",
        media_downloaded=False,
        failure=DigestFailure(
            stage=failure.stage,
            code=failure.code,
            message=failure.message,
            retryable=failure.retryable,
            exit_status=failure.exit_status,
            stderr_summary=failure.stderr_summary,
        ),
    )


def _placeholder_metadata(video_id: str) -> VideoMetadata:
    return VideoMetadata(
        video_id=video_id,
        title=video_id,
        channel=None,
        duration_seconds=None,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
    )


@dataclass(frozen=True)
class _WatchContext:
    duration_seconds: int | None
    original_caption_language: str | None


def _watch_context_from_page(page: str) -> _WatchContext:
    player = _initial_player_response(page)
    if player is None:
        return _WatchContext(None, None)
    duration = _positive_int(_nested_value(player, "videoDetails", "lengthSeconds"))
    renderer = _nested_value(
        player,
        "captions",
        "playerCaptionsTracklistRenderer",
    )
    if not isinstance(renderer, dict):
        return _WatchContext(duration, None)
    caption_tracks = renderer.get("captionTracks")
    if not isinstance(caption_tracks, list) or not caption_tracks:
        return _WatchContext(duration, None)
    default_index = _default_caption_track_index(renderer.get("audioTracks"))
    if default_index is None and len(caption_tracks) == 1:
        default_index = 0
    if default_index is None or not 0 <= default_index < len(caption_tracks):
        return _WatchContext(duration, None)
    default_track = caption_tracks[default_index]
    if not isinstance(default_track, dict):
        return _WatchContext(duration, None)
    language = default_track.get("languageCode")
    return _WatchContext(
        duration,
        language if isinstance(language, str) and language.strip() else None,
    )


def _initial_player_response(page: str) -> dict[str, Any] | None:
    for marker in (
        "ytInitialPlayerResponse =",
        "var ytInitialPlayerResponse =",
        'window["ytInitialPlayerResponse"] =',
    ):
        marker_index = page.find(marker)
        if marker_index < 0:
            continue
        object_start = page.find("{", marker_index + len(marker))
        if object_start < 0:
            continue
        rendered = _balanced_json_object(page, object_start)
        if rendered is None:
            continue
        try:
            value = json.loads(rendered)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _balanced_json_object(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _nested_value(value: Any, *keys: str) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _default_caption_track_index(value: Any) -> int | None:
    if not isinstance(value, list):
        return None
    candidates = [track for track in value if isinstance(track, dict)]
    default_tracks = [track for track in candidates if track.get("hasDefaultTrack") is True]
    if default_tracks:
        selected_tracks = default_tracks
    elif len(candidates) == 1:
        selected_tracks = candidates
    else:
        return None
    for track in selected_tracks:
        index = track.get("defaultCaptionTrackIndex")
        if isinstance(index, int) and not isinstance(index, bool):
            return index
    return None


def _positive_int(value: Any) -> int | None:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number > 0 else None


def _same_language(first: str, second: str) -> bool:
    return first.strip().replace("_", "-").casefold() == second.strip().replace("_", "-").casefold()


def _classify_transcript_api_failure(
    stage: str,
    error: Exception,
) -> YouTubeGatewayFailure:
    error_name = type(error).__name__
    if error_name in {"TranscriptsDisabled", "NoTranscriptFound"}:
        return YouTubeGatewayFailure(
            stage=stage,
            code="captions_unavailable",
            message="The video does not expose a usable caption track.",
            retryable=False,
        )
    if error_name in {"RequestBlocked", "IpBlocked"}:
        return YouTubeGatewayFailure(
            stage=stage,
            code="site_blocked",
            message="YouTube blocked the lightweight transcript request. Try another source.",
            retryable=True,
        )
    if error_name in {
        "AgeRestricted",
        "CookieError",
        "CookieInvalid",
        "CookiePathInvalid",
        "PoTokenRequired",
    }:
        return YouTubeGatewayFailure(
            stage=stage,
            code="authentication_required",
            message="YouTube requires an authenticated browser transcript for this video.",
            retryable=False,
        )
    if error_name in {"InvalidVideoId", "VideoUnavailable", "VideoUnplayable"}:
        return YouTubeGatewayFailure(
            stage=stage,
            code="video_unavailable",
            message="The YouTube video is unavailable or access-restricted.",
            retryable=False,
        )
    if error_name == "YouTubeDataUnparsable":
        return YouTubeGatewayFailure(
            stage=stage,
            code="transcript_parse_failed",
            message="YouTube returned transcript data in an unexpected format.",
            retryable=True,
        )
    if "429" in str(error):
        return YouTubeGatewayFailure(
            stage=stage,
            code="rate_limited",
            message="YouTube rate-limited the lightweight transcript request.",
            retryable=True,
        )
    return YouTubeGatewayFailure(
        stage=stage,
        code="transcript_api_failed",
        message="The lightweight YouTube transcript request failed.",
        retryable=True,
    )


_MAX_OEMBED_BYTES = 1024 * 1024
_MAX_WATCH_BYTES = 6 * 1024 * 1024
