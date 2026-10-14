from types import SimpleNamespace

import pytest

import app.bot.telegram_bot as tb
from app.services import missav
from app.services.missav import (
    candidate_page_urls,
    is_missav_url,
    media_url_from_page,
    resolve_missav_video,
)
from app.services.video_sites import (
    is_adult_video_url,
    requires_ytdlp_impersonation,
    video_platform_label,
)

PAGE_URL = "https://missav.ws/dm70/en/dasd-132"
PLAYLIST = "https://surrit.com/9f03bf15-2cea-4ec8-a472-3243d791c2bc/playlist.m3u8"
# The real packed player script from missav.live, trimmed to the source definitions.
PACKED_PAGE = (
    "<script>eval(function(p,a,c,k,e,d){}("
    "'f=\\'8://7.6/5-4-3-2-1/0.9\\';e=\\'8://7.6/5-4-3-2-1/b/a.9\\';',16,16,"
    "'playlist|3243d791c2bc|a472|4ec8|2cea|9f03bf15|com|surrit|https|m3u8|video|842x480"
    "||||source'.split('|'),0,{}))</script>"
)


class _Response:
    def __init__(self, text: str, url: str, status_code: int = 200):
        self.text = text
        self.url = url
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, pages: dict[str, _Response]):
        self.pages = pages
        self.requested: list[str] = []

    def get(self, url, timeout=None, headers=None):
        self.requested.append(url)
        return self.pages[url]


def test_missav_url_detection_and_site_listing():
    for url in (
        PAGE_URL,
        "https://missav.ai/en/dasd-132",
        "https://www.missav.com/dasd-132",
        "https://missav01.com/dm70/en/dasd-132",
        "https://njavtv.com/dm2/en/dasd-205",
    ):
        assert is_missav_url(url), url
        assert is_adult_video_url(url), url
        assert requires_ytdlp_impersonation(url), url
    assert video_platform_label("https://missav.ai/en/dasd-132") == "MissAV"
    assert not is_missav_url("https://notmissav.ws/en/dasd-132")
    assert not requires_ytdlp_impersonation("https://www.javhdporn.net/video/dasd-132/")


def test_ytdlp_options_impersonate_for_missav_only():
    from yt_dlp.networking.impersonate import ImpersonateTarget

    resolved = SimpleNamespace(referer="https://missav.live/dm70/en/dasd-132")
    opts = tb.ytdlp_common_options(PAGE_URL, resolved)
    assert opts["impersonate"] == ImpersonateTarget.from_str("chrome")
    assert opts["http_headers"]["Referer"] == "https://missav.live/dm70/en/dasd-132"
    youtube_opts = tb.ytdlp_common_options("https://youtu.be/x", SimpleNamespace(referer=None))
    assert "impersonate" not in youtube_opts


def test_candidate_page_urls_try_original_then_mirrors():
    assert candidate_page_urls(PAGE_URL) == (
        PAGE_URL,
        "https://missav.live/dm70/en/dasd-132",
        "https://missav123.com/dm70/en/dasd-132",
        "https://missav01.com/dm70/en/dasd-132",
        "https://njavtv.com/dm70/en/dasd-132",
    )
    assert candidate_page_urls("https://www.missav123.com/en/x")[:2] == (
        "https://www.missav123.com/en/x",
        "https://missav.live/en/x",
    )
    assert "https://missav123.com/en/x" not in candidate_page_urls("https://www.missav123.com/en/x")


def test_media_url_from_page_unpacks_and_prefers_playlist():
    assert media_url_from_page(PACKED_PAGE) == PLAYLIST


def test_media_url_from_page_falls_back_to_literal_playlist():
    escaped = PLAYLIST.replace("/", "\\/")
    html = f'<script>var x = "{escaped}";</script>'
    assert media_url_from_page(html) == PLAYLIST
    assert media_url_from_page("<html>ThisAV landing page</html>") is None


def test_resolve_falls_back_to_mirror_and_returns_its_referer(monkeypatch):
    session = _Session(
        {
            PAGE_URL: _Response("Just a moment...", PAGE_URL, status_code=403),
            "https://missav.live/dm70/en/dasd-132": _Response(
                "<html>home</html>", "https://missav.live/"
            ),
            "https://missav123.com/dm70/en/dasd-132": _Response(
                PACKED_PAGE, "https://missav123.com/dm70/en/dasd-132"
            ),
        }
    )
    monkeypatch.setattr(missav, "_new_session", lambda: session)

    assert resolve_missav_video(PAGE_URL) == (PLAYLIST, "https://missav123.com/dm70/en/dasd-132")
    assert session.requested == [
        PAGE_URL,
        "https://missav.live/dm70/en/dasd-132",
        "https://missav123.com/dm70/en/dasd-132",
    ]


def test_resolve_reports_every_mirror_when_all_fail(monkeypatch):
    pages = {url: _Response("", url, status_code=403) for url in candidate_page_urls(PAGE_URL)}
    monkeypatch.setattr(missav, "_new_session", lambda: _Session(pages))

    with pytest.raises(RuntimeError, match="Could not resolve a MissAV media URL") as error:
        resolve_missav_video(PAGE_URL)
    assert "missav.ws: HTTP 403" in str(error.value)
    assert "njavtv.com: HTTP 403" in str(error.value)
