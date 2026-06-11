from app.downloaders.jdownloader import JDownloaderProvider


async def test_jdownloader_requires_api_url_to_handle_links():
    provider = JDownloaderProvider("")

    assert not await provider.can_handle("https://example.com/file.dlc")


async def test_jdownloader_detects_container_and_explicit_urls():
    provider = JDownloaderProvider("http://127.0.0.1:3129", "secret")

    assert await provider.can_handle("https://example.com/file.dlc")
    assert await provider.can_handle("jd:https://example.com/protected")
    assert not await provider.can_handle("https://example.com/file.zip")


def test_jdownloader_auth_header():
    provider = JDownloaderProvider("http://127.0.0.1:3129", "secret")

    assert provider._headers() == {"Authorization": "Bearer secret"}
