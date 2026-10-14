import base64
import json

import pytest
from Cryptodome.Cipher import AES

from app.services import javcc
from app.services.adult_video_resolver import resolve_adult_video_url
from app.services.javcc import (
    DATALINK_XOR_KEY,
    decode_datalink,
    decrypt_cryptojs_json,
    is_javcc_url,
    media_url_from_player_page,
    player_page_urls,
    resolve_javcc_video_url,
)
from app.services.video_sites import is_adult_video_url, video_platform_label

PAGE_URL = "https://javcc.me/dasd-132-rm"
M3U8_URL = "https://api.videplay.us/x/v-master.m3u8/VTJGc2RHVmtYMSsxR0NMeTlO"
POX = "WuelwhzPR92+50bDErsFZ1h+xjoUQxGl"  # passphrase slice: "0bDErsFZ1h"


def _encode_datalink(url: str, key: bytes = DATALINK_XOR_KEY) -> str:
    xored = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(url.encode()))
    return base64.urlsafe_b64encode(xored).decode().rstrip("=")[::-1]


def _cryptojs_blob(plaintext: str, passphrase: str, salt: bytes = b"\x01" * 8) -> str:
    key, iv = javcc._evp_bytes_to_key(passphrase.encode(), salt, 32, 16)
    data = plaintext.encode()
    pad = 16 - len(data) % 16
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(data + bytes([pad]) * pad)
    payload = {"ct": base64.b64encode(ciphertext).decode(), "iv": iv.hex(), "s": salt.hex()}
    return base64.b64encode(json.dumps(payload).encode()).decode()


def _player_html(media_url: str = M3U8_URL, pox: str = POX, passphrase: str | None = None) -> str:
    blob = _cryptojs_blob(json.dumps(media_url), passphrase or pox[13:23])
    return f"<script>\nlet pox = '{pox}';\nlet dp = '{blob}';\n</script>"


class _Response:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, pages: dict[str, _Response]):
        self.pages = pages
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, timeout=None, headers=None):
        self.requests.append((url, headers or {}))
        return self.pages[url]


def test_javcc_url_detection_and_site_listing():
    assert is_javcc_url(PAGE_URL)
    assert is_javcc_url("https://www.javcc.me/xx/SqX8dvd238k22HFB")
    assert not is_javcc_url("https://notjavcc.me/dasd-132-rm")
    assert is_adult_video_url(PAGE_URL)
    assert video_platform_label(PAGE_URL) == "JAVCC"


def test_decode_real_datalink_tokens():
    assert (
        decode_datalink("3gCJRllCU91UFEQBAkjJpI0MNEUCOUkAPoBALglTCIxJOkxI")
        == "https://javcc.me/xx/SqX8dvd238k22HFB"
    )
    assert (
        decode_datalink("HhhCusDDd8yI0QRMSJyGRI0MNEUCOUkAPoBALglTCIxJOkxI")
        == "https://javcc.me/xx/kLCjPcUBCqmPMfv2"
    )
    assert decode_datalink("!!!not-base64!!!") is None


def test_decode_datalink_recovers_rotated_key():
    token = _encode_datalink("https://javcc.me/xx/AbCdEf123456", key=b"newKey123")
    assert decode_datalink(token) == "https://javcc.me/xx/AbCdEf123456"


def test_player_page_urls_dedupes_attribute_and_buttons():
    first = _encode_datalink("https://javcc.me/xx/first")
    second = _encode_datalink("https://javcc.me/xx/second")
    html = (
        f'<div class="box-server" data-server-urls="[&quot;{first}&quot;,&quot;{second}&quot;]">'
        f'<a href="javascript:void(0)" datalink="{first}">VG</a>'
        f'<a href="javascript:void(0)" datalink="{second}">HV</a></div>'
    )
    assert player_page_urls(html, PAGE_URL) == (
        "https://javcc.me/xx/first",
        "https://javcc.me/xx/second",
    )


def test_decrypt_cryptojs_json_round_trip_and_wrong_key():
    blob = _cryptojs_blob('"https://example.com/a.m3u8"', "secret")
    assert decrypt_cryptojs_json(blob, "secret") == '"https://example.com/a.m3u8"'
    with pytest.raises(ValueError):
        decrypt_cryptojs_json(blob, "not-the-key")


def test_media_url_from_player_page_uses_pox_slice():
    assert media_url_from_player_page(_player_html()) == M3U8_URL


def test_media_url_from_player_page_falls_back_when_slice_moves():
    html = _player_html(passphrase=POX[2:12])
    assert media_url_from_player_page(html) == M3U8_URL


def test_media_url_from_player_page_without_blob():
    assert media_url_from_player_page("<html>no player</html>") is None


def test_resolve_skips_broken_mirror(monkeypatch):
    broken = "https://javcc.me/xx/broken"
    working = "https://javcc.me/xx/working"
    page = "".join(f'<a datalink="{_encode_datalink(url)}">x</a>' for url in (broken, working))
    session = _Session(
        {
            PAGE_URL: _Response(page),
            broken: _Response("", status_code=404),
            working: _Response(_player_html()),
        }
    )
    monkeypatch.setattr(javcc, "_new_session", lambda: session)

    assert resolve_javcc_video_url(PAGE_URL) == M3U8_URL
    assert session.requests[-1] == (
        working,
        {"Accept-Language": "en-US,en;q=0.9", "Referer": PAGE_URL},
    )


def test_resolve_player_page_directly(monkeypatch):
    player = "https://javcc.me/xx/SqX8dvd238k22HFB"
    session = _Session({player: _Response(_player_html())})
    monkeypatch.setattr(javcc, "_new_session", lambda: session)

    assert resolve_javcc_video_url(player) == M3U8_URL


def test_resolve_raises_without_servers(monkeypatch):
    session = _Session({PAGE_URL: _Response("<html></html>")})
    monkeypatch.setattr(javcc, "_new_session", lambda: session)

    with pytest.raises(RuntimeError, match="player servers"):
        resolve_javcc_video_url(PAGE_URL)


def test_adult_video_resolver_routes_javcc(monkeypatch):
    monkeypatch.setattr(
        "app.services.adult_video_resolver.resolve_javcc_video_url",
        lambda url, timeout: M3U8_URL,
    )
    resolved = resolve_adult_video_url(PAGE_URL)
    assert resolved.url == M3U8_URL
    assert resolved.referer == PAGE_URL
