from __future__ import annotations

from collections.abc import Sequence

from .domain import CaptionTrack


def choose_caption_track(
    tracks: Sequence[CaptionTrack],
    preferred_languages: tuple[str, ...],
) -> CaptionTrack:
    if not tracks:
        raise ValueError("The video has no caption tracks")
    return min(
        tracks,
        key=lambda track: _track_rank(track, preferred_languages),
    )


def _track_rank(
    track: CaptionTrack,
    preferred_languages: tuple[str, ...],
) -> tuple[int, int, int, str, str]:
    language_rank = _preferred_language_rank(track.language_code, preferred_languages)
    has_preferred_language = language_rank is not None
    if has_preferred_language:
        category = 0 if not track.is_generated else 1
    elif track.is_original:
        category = 2 if not track.is_generated else 3
    else:
        category = 4 if not track.is_generated else 5
    preference_index, affinity = language_rank or (len(preferred_languages), 99)
    return (
        category,
        preference_index,
        affinity,
        _normalize_language(track.language_code),
        track.identifier,
    )


def _preferred_language_rank(
    candidate: str,
    preferred_languages: tuple[str, ...],
) -> tuple[int, int] | None:
    matches = (
        (index, affinity)
        for index, preferred in enumerate(preferred_languages)
        if (affinity := _language_affinity(preferred, candidate)) is not None
    )
    return min(matches, default=None)


def _language_affinity(preferred: str, candidate: str) -> int | None:
    normalized_preferred = _normalize_language(preferred)
    normalized_candidate = _normalize_language(candidate)
    if normalized_preferred == normalized_candidate:
        return 0
    aliases = _LANGUAGE_ALIASES.get(normalized_preferred, frozenset())
    if normalized_candidate in aliases:
        return 1
    if normalized_preferred.split("-", 1)[0] == normalized_candidate.split("-", 1)[0]:
        return 2
    return None


def _normalize_language(language: str) -> str:
    return language.strip().replace("_", "-").casefold()


_LANGUAGE_ALIASES = {
    "zh-hans": frozenset({"zh", "zh-cn", "zh-sg"}),
    "zh-hant": frozenset({"zh", "zh-hk", "zh-mo", "zh-tw"}),
}
