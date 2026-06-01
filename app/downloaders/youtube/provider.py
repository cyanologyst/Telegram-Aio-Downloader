"""yt-dlp based downloader provider scaffold.

The legacy Telegram runtime still owns the full yt-dlp interaction today.
This provider captures the extension contract for the next extraction phase
without changing the user-facing behavior.
"""

from __future__ import annotations

import re

from app.downloaders.base import BaseDownloader, DownloadRequest
from app.models.download import DownloadResult


class YoutubeDownloader(BaseDownloader):
    """Provider placeholder for yt-dlp-supported video URLs."""

    provider_name = "youtube"
    _url_pattern = re.compile(
        r"(youtube\.com|youtu\.be|vimeo\.com|twitter\.com|x\.com|instagram\.com|tiktok\.com)",
        re.IGNORECASE,
    )

    async def can_handle(self, url: str) -> bool:
        return bool(self._url_pattern.search(url))

    async def download(self, request: DownloadRequest) -> DownloadResult:
        raise NotImplementedError(
            "yt-dlp execution currently lives in app.bot.telegram_bot and will be "
            "migrated behind this provider in the next extraction."
        )
