import pytest

from app.services import adult_video_resolver
from app.services.adult_video_resolver import (
    ResolvedAdultVideo,
    _decode_packed_javascript,
    _media_urls,
    _resolve_generic_adult_media_url,
    _resolve_javtiful_url,
    _resolve_missav_like_url,
    resolve_adult_video_url,
    resolved_video_output_template,
)


def test_decode_packed_javascript_extracts_playlist_url():
    html = """
        <script>
        eval(function(p,a,c,k,e,d){}(
            '0="1://2.3/4.5"',10,6,
            'source|https|cdn|example|playlist|m3u8'.split('|'),0,{}
        ))
        </script>
    """

    assert _decode_packed_javascript(html) == ('source="https://cdn.example/playlist.m3u8"',)


def test_media_urls_normalize_escaped_urls():
    assert _media_urls(r'"https:\/\/cdn.example\/video.mp4?token=a\u0026expires=1"') == (
        "https://cdn.example/video.mp4?token=a&expires=1",
    )


def test_resolve_missav_like_url_prefers_playlist(monkeypatch):
    html = """
        <script>
        eval(function(p,a,c,k,e,d){}(
            '0="1://2.3/4.5"',10,6,
            'source|https|cdn|example|playlist|m3u8'.split('|'),0,{}
        ))
        </script>
    """

    monkeypatch.setattr(adult_video_resolver, "_fetch_page", lambda url, timeout: html)

    assert (
        _resolve_missav_like_url("https://missav.ws/en/example", timeout=1)
        == "https://cdn.example/playlist.m3u8"
    )


def test_resolve_javtiful_url_prefers_largest_player_source(monkeypatch):
    html = """
        <script id="frontWatchConfig" type="application/json">
        {
            "playerSources": [
                {"src": "https://cdn.example/video-720.mp4", "size": "720p"},
                {"src": "https://cdn.example/video-1080.mp4?token=a\\u0026expires=1", "size": 1080}
            ]
        }
        </script>
    """

    monkeypatch.setattr(adult_video_resolver, "_fetch_page", lambda url, timeout: html)

    assert (
        _resolve_javtiful_url("https://javtiful.com/video/1/example", timeout=1)
        == "https://cdn.example/video-1080.mp4?token=a&expires=1"
    )


def test_resolve_generic_adult_media_url_uses_packed_playlist(monkeypatch):
    html = """
        <script>
        eval(function(p,a,c,k,e,d){}(
            '0="1://2.3/4.5"',10,6,
            'source|https|cdn|example|playlist|m3u8'.split('|'),0,{}
        ))
        </script>
    """

    monkeypatch.setattr(adult_video_resolver, "_fetch_page", lambda url, timeout: html)

    assert (
        _resolve_generic_adult_media_url("https://nonktube.com/video/example/", timeout=1)
        == "https://cdn.example/playlist.m3u8"
    )


def test_resolve_generic_adult_media_url_falls_back_to_direct_media(monkeypatch):
    html = """
        <video src="https://cdn.example/video-720.mp4"></video>
        <a href="https://cdn.example/previews/video_preview.mp4">preview</a>
    """

    monkeypatch.setattr(adult_video_resolver, "_fetch_page", lambda url, timeout: html)

    assert (
        _resolve_generic_adult_media_url("https://alphaporno.com/videos/example/", timeout=1)
        == "https://cdn.example/video-720.mp4"
    )


def test_resolve_adult_video_url_returns_referer_for_javtiful(monkeypatch):
    monkeypatch.setattr(
        adult_video_resolver,
        "_resolve_javtiful_url",
        lambda url, timeout: "https://cdn.example/video.mp4",
    )

    assert resolve_adult_video_url("https://javtiful.com/video/1/example") == ResolvedAdultVideo(
        "https://cdn.example/video.mp4",
        referer="https://javtiful.com/video/1/example",
    )


def test_resolve_adult_video_url_returns_referer_for_generic_jav_site(monkeypatch):
    monkeypatch.setattr(
        adult_video_resolver,
        "_resolve_generic_adult_media_url",
        lambda url, timeout: "https://cdn.example/video.m3u8",
    )

    assert resolve_adult_video_url("https://nonktube.com/video/example/") == ResolvedAdultVideo(
        "https://cdn.example/video.m3u8",
        referer="https://nonktube.com/video/example/",
    )


def test_resolve_adult_video_url_routes_missav_mirrors(monkeypatch):
    monkeypatch.setattr(
        adult_video_resolver,
        "_resolve_missav_like_url",
        lambda url, timeout: "https://cdn.example/playlist.m3u8",
    )

    assert resolve_adult_video_url("https://missav.live/en/example") == ResolvedAdultVideo(
        "https://cdn.example/playlist.m3u8",
        referer="https://missav.live/en/example",
    )


def test_resolve_adult_video_url_leaves_unknown_urls_unchanged():
    assert resolve_adult_video_url("https://example.com/video") == ResolvedAdultVideo(
        "https://example.com/video"
    )


def test_resolved_video_output_template_avoids_signed_url_ids(tmp_path):
    template = resolved_video_output_template(
        tmp_path,
        "https://javtiful.com/video/107145/mida-625",
    )

    assert "%(id)s" not in template
    assert "%(title).160B" in template
    assert "X-Amz" not in template
    assert len(template) < len(str(tmp_path)) + 200


def test_resolve_missav_like_url_raises_when_page_has_no_media(monkeypatch):
    monkeypatch.setattr(adult_video_resolver, "_fetch_page", lambda url, timeout: "<html></html>")

    with pytest.raises(RuntimeError, match="Could not resolve"):
        _resolve_missav_like_url("https://missav.ws/en/example", timeout=1)
