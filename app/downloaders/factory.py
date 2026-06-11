"""Factory for assembling downloader providers from settings."""

from __future__ import annotations

from app.config.settings import Settings
from app.downloaders.gallery import GalleryDlDownloader
from app.downloaders.hentai import HanimePluginDownloader
from app.downloaders.jdownloader import JDownloaderProvider
from app.downloaders.registry import DownloaderRegistry
from app.downloaders.spotify import SpotifyDownloader
from app.downloaders.youtube import YoutubeDownloader


def build_downloader_registry(settings: Settings) -> DownloaderRegistry:
    """Build the default provider registry in resolution order."""

    registry = DownloaderRegistry()
    registry.register(HanimePluginDownloader())
    registry.register(SpotifyDownloader(ffmpeg_bin=settings.ffmpeg_bin))
    registry.register(GalleryDlDownloader(gallery_dl_bin=settings.gallery_dl_bin))
    registry.register(
        JDownloaderProvider(
            api_url=settings.jdownloader_api_url,
            api_token=settings.jdownloader_api_token,
        )
    )
    registry.register(YoutubeDownloader())
    return registry
