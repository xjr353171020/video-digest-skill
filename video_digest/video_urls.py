from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class VideoReference:
    platform: str
    video_id: str
    canonical_url: str
    bvid: str | None = None
    page: int | None = None


def video_reference(url: str) -> VideoReference:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":", 1)[0]
    if host in {"youtu.be", "www.youtu.be"} or _is_domain(host, "youtube.com"):
        return _youtube_reference(parsed, host)
    if _is_domain(host, "bilibili.com"):
        return _bilibili_reference(parsed)
    raise ValueError("The URL is not a supported YouTube or Bilibili video URL")


def bilibili_video_reference(url: str) -> VideoReference:
    reference = video_reference(url)
    if reference.platform != "bilibili":
        raise ValueError("The URL does not contain a valid Bilibili BV video ID")
    return reference


def _youtube_reference(parsed: object, host: str) -> VideoReference:
    path = parsed.path.strip("/")  # type: ignore[attr-defined]
    query = parse_qs(parsed.query)  # type: ignore[attr-defined]
    if host in {"youtu.be", "www.youtu.be"}:
        video_id = path.split("/", 1)[0]
    else:
        video_id = query.get("v", [""])[0]
        path_parts = path.split("/")
        if not video_id and len(path_parts) >= 2 and path_parts[0] == "shorts":
            video_id = path_parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,}", video_id):
        raise ValueError("The URL does not contain a valid YouTube video ID")
    return VideoReference(
        platform="youtube",
        video_id=video_id,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _bilibili_reference(parsed: object) -> VideoReference:
    match = re.fullmatch(
        r"/?video/(?P<bvid>BV[A-Za-z0-9]{10})/?",
        parsed.path,  # type: ignore[attr-defined]
        flags=re.IGNORECASE,
    )
    if match is None:
        raise ValueError("The URL does not contain a valid Bilibili BV video ID")
    raw_bvid = match.group("bvid")
    bvid = f"BV{raw_bvid[2:]}"
    raw_page = parse_qs(parsed.query).get("p", ["1"])[0]  # type: ignore[attr-defined]
    try:
        page = int(raw_page)
    except ValueError as error:
        raise ValueError("The Bilibili part number must be a positive integer") from error
    if page < 1:
        raise ValueError("The Bilibili part number must be a positive integer")
    return VideoReference(
        platform="bilibili",
        video_id=f"{bvid}_p{page}",
        canonical_url=f"https://www.bilibili.com/video/{bvid}?p={page}",
        bvid=bvid,
        page=page,
    )


def _is_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")
