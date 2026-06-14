import httpx
import pytest

from app.downloaders.torrents.prowlarr import ProwlarrClient, ProwlarrConfigError


@pytest.mark.asyncio
async def test_prowlarr_search_normalizes_releases():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/search"
        assert request.headers["x-api-key"] == "key"
        return httpx.Response(
            200,
            json=[
                {
                    "title": "Ubuntu ISO",
                    "indexer": "Example",
                    "size": 1024,
                    "seeders": 12,
                    "leechers": 2,
                    "downloadUrl": "http://prowlarr/download/1",
                    "categories": [{"name": "PC"}],
                }
            ],
        )

    client = ProwlarrClient("http://prowlarr", "key")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://prowlarr",
        headers={"X-Api-Key": "key"},
    )

    results = await client.search("ubuntu", "apps")

    assert len(results) == 1
    assert results[0]["title"] == "Ubuntu ISO"
    assert results[0]["source_url"] == "http://prowlarr/download/1"
    assert results[0]["categories"] == ["PC"]
    await client.close()


@pytest.mark.asyncio
async def test_prowlarr_resolve_magnet_does_not_fetch(tmp_path):
    client = ProwlarrClient("http://prowlarr", "key")

    source, torrent_path = await client.resolve_download_source(
        {"magnet_url": "magnet:?xt=urn:btih:ABC", "title": "Ubuntu"},
        tmp_path,
    )

    assert source == "magnet:?xt=urn:btih:ABC"
    assert torrent_path is None


@pytest.mark.asyncio
async def test_prowlarr_resolve_torrent_download_writes_file(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"d8:announce0:e")

    client = ProwlarrClient("http://prowlarr", "key")
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://prowlarr"
    )

    source, torrent_path = await client.resolve_download_source(
        {"download_url": "http://prowlarr/download/1", "title": "Ubuntu/ISO"},
        tmp_path,
    )

    assert torrent_path is not None
    assert source == str(torrent_path)
    assert torrent_path.read_bytes() == b"d8:announce0:e"
    await client.close()


@pytest.mark.asyncio
async def test_prowlarr_requires_api_key():
    client = ProwlarrClient("http://prowlarr", "")

    with pytest.raises(ProwlarrConfigError):
        await client.search("ubuntu")
