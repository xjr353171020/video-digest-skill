from __future__ import annotations

import pytest

from video_digest.youtube import youtube_video_id


@pytest.mark.parametrize(
    "url",
    (
        "https://www.youtube.com/shorts/2EjWPYeaDxI",
        "https://youtube.com/shorts/2EjWPYeaDxI?feature=share",
        "https://m.youtube.com/shorts/2EjWPYeaDxI/",
    ),
)
def test_youtube_video_id_accepts_shorts_urls(url: str) -> None:
    assert youtube_video_id(url) == "2EjWPYeaDxI"
