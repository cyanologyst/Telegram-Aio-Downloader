import pytest

from app.downloaders.base import DownloadRequest
from app.downloaders.youtube.provider import YoutubeDownloader


async def test_youtube_provider_detects_supported_urls(tmp_path):
    provider = YoutubeDownloader()

    assert await provider.can_handle("https://www.youtube.com/watch?v=abc")
    assert await provider.can_handle("https://youtu.be/abc")
    assert not await provider.can_handle("magnet:?xt=urn:btih:test")


async def test_youtube_provider_download_is_explicitly_not_migrated(tmp_path):
    provider = YoutubeDownloader()

    with pytest.raises(NotImplementedError):
        await provider.download(DownloadRequest(url="https://youtu.be/abc", destination=tmp_path))
