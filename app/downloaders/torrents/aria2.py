"""aria2 torrent downloader provider scaffold."""

from __future__ import annotations

from app.downloaders.base import BaseDownloader, DownloadRequest
from app.models.download import DownloadResult


class Aria2TorrentDownloader(BaseDownloader):
    """Provider placeholder for magnet and .torrent downloads."""

    provider_name = "aria2"

    async def can_handle(self, url: str) -> bool:
        return url.startswith("magnet:") or url.lower().endswith(".torrent")

    async def download(self, request: DownloadRequest) -> DownloadResult:
        raise NotImplementedError(
            "aria2 process management currently lives in app.bot.telegram_bot and "
            "will be migrated behind this provider in the next extraction."
        )
