import base64
import json

import pytest

from app.services import javhdporn
from app.services.javhdporn import (
    API_SALT,
    MPU_SALTS,
    PLAYER_CONFIG_SALT,
    _candidate_video_pages,
    _code_from_url,
    _matching_media_url,
    _seed_key,
    is_javhdporn_url,
    media_url_from_player_page,
    play_request_from_page,
    player_urls_from_api,
    rc4_decrypt,
    resolve_javhdporn_video_url,
)


def test_javhdporn_url_detection():
    assert is_javhdporn_url("https://www.javhdporn.net/video/pppd-680-decensored/")
    assert is_javhdporn_url("https://javhdporn.net/v1/video/pppd-680/")
    assert not is_javhdporn_url("https://example.com/video/pppd-680/")


def test_code_extraction_from_video_url():
    assert _code_from_url("https://www.javhdporn.net/video/pppd-680-decensored/") == "pppd-680"
    assert _code_from_url("https://www.javhdporn.net/v1/video/pppd00680/") == "pppd-680"


def test_candidate_video_pages_are_filtered_to_same_code():
    html = """
        <a href="https://www.javhdporn.net/v1/video/pppd-680/">same code</a>
        <a href="https://www.javhdporn.net/video/pppd-636-decensored/">related</a>
    """

    assert _candidate_video_pages(
        html,
        "https://www.javhdporn.net/video/pppd-680-decensored/",
        "pppd-680",
    ) == ("https://www.javhdporn.net/v1/video/pppd-680/",)


def test_matching_media_url_prefers_same_code_and_ignores_previews():
    html = """
        https://video.pornfhd.com/v/censored/181936_PPPD-636.mp4
        https://video.pornfhd.com/p/8/c8/pppd-680/preview.png
        https://video.pornfhd.com/v/censored/191101_PPPD-680.mp4
    """

    assert (
        _matching_media_url(html, "pppd-680")
        == "https://video.pornfhd.com/v/censored/191101_PPPD-680.mp4"
    )



# --- Player API chain (data-mpu -> /api/play/ -> /p/<id> data-config) ---

PAGE_URL = "https://www.javhdporn.net/video/dasd-132-decensored/"
VIDEO_ID = "813776"
# Captured live: the /api/play/ "data" field for video 813776 and what cast.js made of it.
REAL_API_DATA = (
    "xLi8oHlEgpc1uPaz0Q8XoRj1EJ8QRN1FlZQ6i0qs40Rg2TkOFowP2eRps7nOCX8ltVaxueaoG5ieH6vUrtb8xeL"
    "\\/XI3tbht8q1J4Ay4N4fHhO1jP"
)
REAL_PLAYER_URL = "https://video1.javhdporn.net/p/sxzq5zlfja9r?t=8580ae3fee&s=1791582123"
# Captured live: the player page's data-config and the source main.js handed to jwplayer.
REAL_DATA_CONFIG = (
    "hONUlGzfIrBdPH8N+oQ+EDI9sj6uILVUqmB95ETxHOLokSNBVFSE7VbmC01YlqdRozmg8yExCr4GZskeRO94Jngq"
    "URFA1bCvYY/e/lFymT5IKDkIs2fEHLwHOcl2iNRtz7I9joR222jb4sdBMVL2hdP2dvtjR3V0Kl5rmnLbgEqEz9OR"
    "x+lWtdtOTyw3INg6eQ6zzZP3lyC50n2Hf/S9S862GUMIVyLGpmQeNiqhjMLVFOFa4gvPsbhmsgu6Ou5OSqrR++JS"
    "I+Mw4udNJrS8s5LYS5aeCaWNzywS4m4/4AP2tPkOeph0xwC/ArsIKDqe82TGwZ3yiLcrxUVZgKznwpE1et8z+HU7"
    "JvTHdWvPYWIDDbTyvRPx+K4Mblz5X8XxHU3HHzXfI/fl2HR0CfCtIZzgQ1F147IreQ+LAEj4c6bg6EP5KqEpbXJm"
    "26cGPXCH+aMr8OGDrVH8AoptjZzzdQQiaqxPjh5u+axWFk4B/+Q11ZLvQW6YBEoS0oOOS72WtYrJC3fBNd/9Du9o"
    "TZumab1PjWoCfHeb6g0BbpZ9InmnaZzBei6uVyfYP3r4LBtFnMRgNYyyk4jCLZmclEolVCkK5b/NP9UdIGd6Twdp"
    "3SDuw9ftrz8PG/Tmc39HDMJ3XjBFhw0YMKUt1CPrgDSvdieTCs0="
)
REAL_M3U8 = (
    "https://streamhls.click/hls/RqsYavGS6VHG5sNjd437xA/kjhhiuahiuhgihdf"
    "/1791625432/73122962/master.m3u8"
)


def _rc4_encrypt(seed: str, salt: str, plaintext: str) -> str:
    """Inverse of the site's dex(): btoa(rc4(key, btoa(plaintext)))."""

    key = _seed_key(seed, salt)
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    i = j = 0
    out = bytearray()
    for byte in base64.b64encode(plaintext.encode()):
        i = (i + 1) % 256
        j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return base64.b64encode(bytes(out)).decode()


def _page_html(token: str = "hVzwA35nqP0GZAH-jVyRu3Khc-t7Eu4AGQ", ver: str = "2") -> str:
    mpu = _rc4_encrypt(VIDEO_ID, MPU_SALTS[ver], token)
    return (
        f'<div class="video-player-area" id="video-player-area" data-video-id="{VIDEO_ID}">'
        f'<div class="video-player" id="video-player" data-mpu="{mpu}" data-ver="{ver}">'
        "</div></div>"
        '<a href="https://video.pornfhd.com/v/censored/181936_PPPD-636.mp4">related</a>'
    )


def _player_html(player_url: str, media_url: str) -> str:
    path = player_url.split("?")[0].split("javhdporn.net", 1)[1]
    seed = base64.b64encode(path.encode()).decode()[4:]
    sources = base64.b64encode(json.dumps([{"file": media_url, "type": "hls"}]).encode()).decode()
    config_json = json.dumps({"src": sources, "img": "", "vtt": ""})
    config = _rc4_encrypt(seed, PLAYER_CONFIG_SALT, config_json)
    return f'<div id="jwplayer" data-config="{config}"></div>'


class _Response:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, pages: dict[str, _Response], api: _Response):
        self.pages = pages
        self.api = api
        self.posts: list[tuple[str, dict, dict]] = []
        self.gets: list[tuple[str, dict]] = []

    def get(self, url, timeout=None, headers=None):
        self.gets.append((url, headers or {}))
        return self.pages[url]

    def post(self, url, data=None, timeout=None, headers=None):
        self.posts.append((url, data, headers or {}))
        return self.api


def test_rc4_decrypts_real_api_blob():
    plain = rc4_decrypt(_seed_key(VIDEO_ID, API_SALT), REAL_API_DATA.replace("\\/", "/"))
    assert "https:" + plain == REAL_PLAYER_URL
    assert rc4_decrypt(_seed_key("1", API_SALT), REAL_API_DATA) is None


def test_media_url_from_real_player_config():
    html = f'<div id="jwplayer" data-config="{REAL_DATA_CONFIG}"></div>'
    assert media_url_from_player_page(html, REAL_PLAYER_URL) == REAL_M3U8
    assert media_url_from_player_page(html, "https://video1.javhdporn.net/p/other") is None
    assert media_url_from_player_page("<html></html>", REAL_PLAYER_URL) is None


@pytest.mark.parametrize("ver", ["1", "2"])
def test_play_request_decrypts_mpu_for_each_version(ver):
    token = "hVzwA35nqP0GZAH-jVyRu3Khc-t7Eu4AGQ_x"
    assert play_request_from_page(_page_html(token, ver)) == (token, ver, VIDEO_ID)


def test_play_request_without_player_raises():
    with pytest.raises(RuntimeError, match="no video player"):
        play_request_from_page("<html></html>")


def test_player_urls_from_api_include_reserve_servers():
    reserve = json.dumps(
        [
            {"data": _rc4_encrypt(VIDEO_ID, API_SALT, "//video2.javhdporn.net/p/b"), "lo": "nl"},
            {"data": _rc4_encrypt(VIDEO_ID, API_SALT, "//video1.javhdporn.net/p/a"), "lo": "de"},
        ]
    )
    payload = {
        "status": True,
        "data": _rc4_encrypt(VIDEO_ID, API_SALT, "//video1.javhdporn.net/p/a"),
        "reserve": _rc4_encrypt(VIDEO_ID, API_SALT, reserve),
    }
    assert player_urls_from_api(payload, VIDEO_ID) == (
        "https://video1.javhdporn.net/p/a",
        "https://video2.javhdporn.net/p/b",
    )
    with pytest.raises(RuntimeError, match="refused"):
        player_urls_from_api({"status": False}, VIDEO_ID)


def test_resolve_uses_player_api_and_skips_broken_server(monkeypatch):
    broken = "https://video1.javhdporn.net/p/broken?t=1"
    working = "https://video2.javhdporn.net/p/working?t=2"
    m3u8 = "https://streamhls.click/hls/abc/master.m3u8"
    api = {
        "status": True,
        "data": _rc4_encrypt(VIDEO_ID, API_SALT, "//video1.javhdporn.net/p/broken?t=1"),
        "reserve": _rc4_encrypt(
            VIDEO_ID, API_SALT, json.dumps([{"data": _rc4_encrypt(VIDEO_ID, API_SALT, working)}])
        ),
    }
    session = _Session(
        {
            PAGE_URL: _Response(_page_html("tok_en-123456789012345")),
            broken: _Response("", status_code=502),
            working: _Response(_player_html(working, m3u8)),
        },
        _Response(json.dumps(api)),
    )
    monkeypatch.setattr(javhdporn, "_new_session", lambda: session)

    assert resolve_javhdporn_video_url(PAGE_URL) == m3u8
    url, data, headers = session.posts[0]
    assert url == "https://www.javhdporn.net/api/play/"
    assert data == {"sources": "tok_en-123456789012345", "ver": "2"}
    assert headers["Referer"] == PAGE_URL
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    assert session.gets[-1] == (working, {"Accept-Language": "en-US,en;q=0.9", "Referer": PAGE_URL})


def test_resolve_reports_player_error_when_no_fallback(monkeypatch):
    session = _Session(
        {PAGE_URL: _Response(_page_html())},
        _Response(json.dumps({"status": False})),
    )
    monkeypatch.setattr(javhdporn, "_new_session", lambda: session)

    with pytest.raises(RuntimeError, match="Player: the play API refused"):
        resolve_javhdporn_video_url(PAGE_URL)
