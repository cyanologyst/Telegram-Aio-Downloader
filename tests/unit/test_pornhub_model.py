import pytest

from app.services import pornhub_model


def test_pornhub_model_url_detection():
    assert pornhub_model.is_pornhub_model_url("https://www.pornhub.com/model/example-model")
    assert pornhub_model.is_pornhub_model_url("https://pornhub.com/model/example_model-123/")
    assert not pornhub_model.is_pornhub_model_url(
        "https://www.pornhub.com/view_video.php?viewkey=abc"
    )
    assert not pornhub_model.is_pornhub_model_url("https://example.com/model/example-model")


@pytest.mark.asyncio
async def test_resolve_pornhub_model_playlist_filters_video_entries(monkeypatch):
    class DummyYoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def extract_info(self, url, download=False):
            return {
                "title": "Example Model",
                "entries": [
                    {"url": "https://www.pornhub.com/view_video.php?viewkey=abc"},
                    {"url": "/view_video.php?viewkey=def"},
                    {"url": "https://example.com/view_video.php?viewkey=nope"},
                    {"url": "https://www.pornhub.com/model/example-model"},
                    {},
                ],
            }

    monkeypatch.setattr(pornhub_model.yt_dlp, "YoutubeDL", DummyYoutubeDL)

    playlist = await pornhub_model.resolve_pornhub_model_playlist(
        "https://www.pornhub.com/model/example-model"
    )

    assert playlist.title == "Example Model"
    assert playlist.site == "PornHub"
    assert playlist.slug == "example-model"
    assert playlist.urls == (
        "https://www.pornhub.com/view_video.php?viewkey=abc",
        "https://www.pornhub.com/view_video.php?viewkey=def",
    )


@pytest.mark.asyncio
async def test_resolve_pornhub_model_playlist_raises_when_empty(monkeypatch):
    class DummyYoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return None

        def extract_info(self, url, download=False):
            return {"title": "Empty Model", "entries": []}

    monkeypatch.setattr(pornhub_model.yt_dlp, "YoutubeDL", DummyYoutubeDL)

    with pytest.raises(RuntimeError, match="No public videos"):
        await pornhub_model.resolve_pornhub_model_playlist(
            "https://www.pornhub.com/model/example-model"
        )
