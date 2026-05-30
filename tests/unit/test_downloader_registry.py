from app.downloaders.base import BaseDownloader, DownloadRequest
from app.downloaders.registry import DownloaderRegistry
from app.models.download import DownloadResult


class DummyDownloader(BaseDownloader):
    provider_name = "dummy"

    async def can_handle(self, url: str) -> bool:
        return url.startswith("dummy:")

    async def download(self, request: DownloadRequest) -> DownloadResult:
        return DownloadResult(provider=self.provider_name, title=request.url)


async def test_registry_resolves_first_matching_provider():
    provider = DummyDownloader()
    registry = DownloaderRegistry([provider])

    assert await registry.resolve("dummy:test") is provider
    assert await registry.resolve("https://example.com") is None

