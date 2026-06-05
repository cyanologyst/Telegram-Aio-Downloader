from app.services.video_sites import (
    is_adult_video_url,
    is_hentai_video_url,
    is_supported_video_url,
    video_platform_label,
    video_platform_slug,
)


def test_general_video_sites_are_supported():
    assert is_supported_video_url("https://www.youtube.com/watch?v=abc")
    assert is_supported_video_url("https://x.com/example/status/123")
    assert not is_adult_video_url("https://www.youtube.com/watch?v=abc")
    assert video_platform_label("https://m.tiktok.com/@user/video/1") == "TikTok"


def test_adult_video_sites_are_supported_and_labeled():
    url = "https://www.pornhub.com/view_video.php?viewkey=abc"

    assert is_supported_video_url(url)
    assert is_adult_video_url(url)
    assert not is_hentai_video_url(url)
    assert video_platform_label(url) == "PornHub"
    assert video_platform_slug(url) == "PornHub"


def test_adult_video_site_subdomains_match():
    url = "https://de.xhamster.com/videos/example"

    assert is_supported_video_url(url)
    assert is_adult_video_url(url)
    assert video_platform_label(url) == "XHamster"


def test_unknown_http_url_is_not_supported_video():
    assert not is_supported_video_url("https://example.com/file.iso")
    assert not is_adult_video_url("magnet:?xt=urn:btih:test")


def test_hentai_video_sites_are_supported_and_labeled():
    url = "https://hstream.moe/hentai/star-jewel-1"

    assert is_supported_video_url(url)
    assert is_hentai_video_url(url)
    assert not is_adult_video_url(url)
    assert video_platform_label(url) == "HStream"
    assert video_platform_slug(url) == "HStream"


def test_inactive_hentai_candidates_are_not_routed():
    assert not is_supported_video_url("https://hanime.tv/videos/hentai/todo-no-tsumari-1")
    assert not is_supported_video_url("https://ohentai.org/detail.php?vid=NDg4")
    assert not is_supported_video_url("https://oppai.stream/watch?e=Example")
