"""JAVCC page resolver.

A javcc.me video page lists its mirrors as obfuscated ``datalink`` tokens. Each
token decodes to a ``/xx/<id>`` JW Player page that ships the HLS URL as a
CryptoJS (OpenSSL "Salted__" AES-CBC) JSON blob in ``dp``; the passphrase is a
fixed slice of the ``pox`` string next to it. The bot decrypts that blob to a
playable m3u8 and lets the existing yt-dlp workflow download it.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
from html import unescape
from urllib.parse import urlparse

DATALINK_RE = re.compile(r"""datalink=["']([A-Za-z0-9_\-+/=]+)["']""")
SERVER_URLS_RE = re.compile(r"""data-server-urls=["'](\[[^"']*\])["']""")
POX_RE = re.compile(r"""\bpox\s*=\s*["']([^"']+)["']""")
DP_RE = re.compile(r"""\bdp\s*=\s*["']([A-Za-z0-9+/=]+)["']""")
# Hard-coded in the page's obfuscated mirror-button script.
DATALINK_XOR_KEY = b"KmzWa8awaallakclnu"
# ``ply(pox, dp)`` decrypts with ``pox.substr(13, 10)``.
POX_KEY_SLICE = slice(13, 23)


def is_javcc_url(url: str) -> bool:
    """Return whether ``url`` targets JAVCC."""

    host = (urlparse(url.strip()).hostname or "").lower()
    return host == "javcc.me" or host.endswith(".javcc.me")


def resolve_javcc_video_url(url: str, timeout: float = 30.0) -> str:
    """Resolve a JAVCC video page (or ``/xx/`` player page) to an HLS URL."""

    if not is_javcc_url(url):
        return url

    session = _new_session()
    if _is_player_page(url):
        player_urls: tuple[str, ...] = (url,)
        referer = url
    else:
        page_html = _fetch_page(url, timeout=timeout, session=session)
        player_urls = player_page_urls(page_html, url)
        referer = url
        if not player_urls:
            raise RuntimeError("Could not find any JAVCC player servers on the page.")

    errors: list[str] = []
    for player_url in player_urls:
        try:
            player_html = _fetch_page(player_url, timeout=timeout, session=session, referer=referer)
            media_url = media_url_from_player_page(player_html)
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            errors.append(f"{player_url}: {exc}")
            continue
        if media_url:
            return media_url
        errors.append(f"{player_url}: no media URL")
    raise RuntimeError("Could not resolve a JAVCC media URL. " + "; ".join(errors))


def player_page_urls(html: str, page_url: str) -> tuple[str, ...]:
    """Decode every mirror ``datalink`` on a JAVCC video page."""

    tokens: list[str] = []
    for match in SERVER_URLS_RE.finditer(html):
        try:
            tokens.extend(str(token) for token in json.loads(unescape(match.group(1))))
        except (ValueError, TypeError):
            continue
    tokens.extend(DATALINK_RE.findall(html))

    host = urlparse(page_url).hostname or "javcc.me"
    urls: list[str] = []
    for token in tokens:
        decoded = decode_datalink(token, host=host)
        if decoded and decoded not in urls:
            urls.append(decoded)
    return tuple(urls)


def decode_datalink(token: str, host: str = "javcc.me") -> str | None:
    """Decode a ``datalink`` token (reversed base64, repeating-key XOR)."""

    raw = _datalink_bytes(token)
    if raw is None:
        return None
    decoded = _xor(raw, DATALINK_XOR_KEY)
    if _looks_like_url(decoded):
        return decoded.decode("ascii")

    # The key lives in obfuscated JS and may rotate; recover it from the known prefix.
    known = f"https://{host}/xx/".encode("ascii")
    for key_length in range(4, len(known) + 1):
        key = bytes(byte ^ plain for byte, plain in zip(raw[:key_length], known, strict=False))
        decoded = _xor(raw, key)
        if decoded.startswith(known) and _looks_like_url(decoded):
            return decoded.decode("ascii")
    return None


def media_url_from_player_page(html: str) -> str | None:
    """Decrypt the ``dp`` blob of a ``/xx/`` JW Player page to its HLS URL."""

    pox_match = POX_RE.search(html)
    dp_match = DP_RE.search(html)
    if not pox_match or not dp_match:
        return None
    pox = pox_match.group(1)
    blob = dp_match.group(1)

    candidates = [pox[POX_KEY_SLICE]]
    # Fall back to every slice of ``pox`` in case the offset moves.
    candidates += [
        pox[start : start + length]
        for length in range(6, len(pox) + 1)
        for start in range(len(pox) - length + 1)
    ]
    for passphrase in candidates:
        try:
            plaintext = decrypt_cryptojs_json(blob, passphrase)
        except ValueError:
            continue
        media_url = _media_url_from_plaintext(plaintext)
        if media_url:
            return media_url
    return None


def decrypt_cryptojs_json(blob: str, passphrase: str) -> str:
    """Decrypt a base64 CryptoJS JSON payload ``{"ct", "iv", "s"}``."""

    try:
        payload = json.loads(base64.b64decode(blob + "=" * (-len(blob) % 4)))
        ciphertext = base64.b64decode(payload["ct"])
        salt = bytes.fromhex(payload["s"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("Not a CryptoJS JSON payload.") from exc
    return decrypt_cryptojs(ciphertext, passphrase, salt)


def decrypt_cryptojs(ciphertext: bytes, passphrase: str, salt: bytes) -> str:
    """CryptoJS ``AES.decrypt(ct, passphrase)``: EVP_BytesToKey(MD5) + AES-256-CBC."""

    try:
        from Cryptodome.Cipher import AES
    except ImportError as exc:
        raise RuntimeError("JAVCC support requires pycryptodomex to be installed.") from exc

    key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), salt, 32, 16)
    if not ciphertext or len(ciphertext) % 16:
        raise ValueError("Bad ciphertext length.")
    padded = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
    pad = padded[-1]
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        raise ValueError("Bad padding; wrong passphrase.")
    try:
        return padded[:-pad].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Wrong passphrase.") from exc


def _evp_bytes_to_key(
    password: bytes, salt: bytes, key_len: int, iv_len: int
) -> tuple[bytes, bytes]:
    derived = b""
    block = b""
    while len(derived) < key_len + iv_len:
        block = hashlib.md5(block + password + salt).digest()
        derived += block
    return derived[:key_len], derived[key_len : key_len + iv_len]


def _media_url_from_plaintext(plaintext: str) -> str | None:
    text = plaintext.strip()
    if text.startswith('"'):
        with contextlib.suppress(ValueError):
            text = str(json.loads(text))
    text = text.replace("\\/", "/")
    if re.match(r"https?://\S+$", text):
        return text
    return None


def _is_player_page(url: str) -> bool:
    return urlparse(url).path.startswith("/xx/")


def _datalink_bytes(token: str) -> bytes | None:
    normalized = token.strip()[::-1].replace("-", "+").replace("_", "/")
    normalized += "=" * (-len(normalized) % 4)
    try:
        return base64.b64decode(normalized, validate=True)
    except ValueError:
        return None


def _xor(data: bytes, key: bytes) -> bytes:
    return bytes(byte ^ key[index % len(key)] for index, byte in enumerate(data))


def _looks_like_url(value: bytes) -> bool:
    return re.fullmatch(rb"https?://[\x21-\x7e]+", value) is not None


def _new_session():
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise RuntimeError("JAVCC support requires curl-cffi to be installed.") from exc

    return requests.Session(impersonate="chrome")


def _fetch_page(url: str, timeout: float, session, referer: str | None = None) -> str:
    headers = {"Accept-Language": "en-US,en;q=0.9"}
    if referer:
        headers["Referer"] = referer
    response = session.get(url, timeout=timeout, headers=headers)
    response.raise_for_status()
    return str(response.text)
