import pytest

from app.services.hentai_playlist import is_hentai_playlist_url, resolve_hentai_playlist


def test_detects_supported_hentai_playlist_urls():
    assert is_hentai_playlist_url("https://hentaihaven.com/video/example-title/")
    assert is_hentai_playlist_url("https://hstream.moe/hentai/example-title")


def test_rejects_single_episode_and_unsupported_playlist_urls():
    assert not is_hentai_playlist_url("https://hentaihaven.com/video/example-title/episode-1")
    assert not is_hentai_playlist_url("https://hstream.moe/hentai/example-title-1")
    assert not is_hentai_playlist_url("https://hentaimama.io/episodes/example-episode-1/")


@pytest.mark.parametrize(
    ("url", "expected_site", "expected_urls"),
    [
        (
            "https://hentaihaven.com/video/example-title/",
            "HentaiHaven",
            [
                "https://hentaihaven.com/video/example-title/episode-1",
                "https://hentaihaven.com/video/example-title/episode-2",
            ],
        ),
        (
            "https://hstream.moe/hentai/example-title",
            "HStream",
            [
                "https://hstream.moe/hentai/example-title-1",
                "https://hstream.moe/hentai/example-title-2",
            ],
        ),
    ],
)
async def test_resolves_playlist_episode_urls(monkeypatch, url, expected_site, expected_urls):
    class DummyResponse:
        text = """
            <html>
              <head><title>Example Title - Hentai Haven</title></head>
              <body>
                <a href="/video/example-title/episode-2">Episode 2</a>
                <a href="/video/example-title/episode-1">Episode 1</a>
                <a href="/video/other-title/episode-1">Other</a>
                <a href="/hentai/example-title-2">Episode 2</a>
                <a href="/hentai/example-title-1">Episode 1</a>
                <a href="/hentai/other-title-1">Other</a>
              </body>
            </html>
        """

        def raise_for_status(self):
            return None

    class DummyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, requested_url):
            assert requested_url == url
            return DummyResponse()

    monkeypatch.setattr("app.services.hentai_playlist.httpx.AsyncClient", DummyClient)

    playlist = await resolve_hentai_playlist(url)

    assert playlist.site == expected_site
    assert playlist.urls == expected_urls
