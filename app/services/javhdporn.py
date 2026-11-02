"""JavHDPorn page resolver.

JavHDPorn pages are Cloudflare-protected. The real player is a
``<div id="video-player" data-mpu=... data-ver=...>`` whose token the site's
``cast.js`` decrypts and POSTs to ``/api/play/``; the encrypted answer is a
``video1.javhdporn.net/p/<id>`` JW Player page whose ``data-config`` blob holds
the HLS URL. Every layer is RC4 keyed on ``reverse(btoa(<seed> + <salt>))`` with
salts hard-coded in the obfuscated JS. Older pages instead link a secondary
video page with a direct MP4, which is kept as the fallback.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
from html import unescape
from urllib.parse import urldefrag, urljoin, urlparse

MEDIA_URL_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:mp4|m3u8)(?:\?[^\s\"'<>]*)?", re.I)
VIDEO_PAGE_RE = re.compile(r"https?://(?:www\.)?javhdporn\.net/(?:v\d+/)?video/[^\"'<> ]+/?", re.I)
SLUG_CODE_RE = re.compile(r"([a-z]{2,10})[-_]?0*(\d{2,6})", re.I)
PLAYER_DIV_RE = re.compile(r"<div\b[^>]*\bid=[\"']video-player[\"'][^>]*>", re.I)
VIDEO_ID_RE = re.compile(r"""\bdata-video-id=["'](\d+)["']""")
DATA_CONFIG_RE = re.compile(r"""\bdata-config=["']([A-Za-z0-9+/=]+)["']""")
PLAY_API_PATH = "/api/play/"
# cast.js keys ``data-mpu`` with a salt picked by ``data-ver``.
MPU_SALTS = {"2": "SyntaxError", "1": "QxLUF1bgIAdeQX"}
# cast.js keys the ``/api/play/`` answer (``data`` and ``reserve``) with this salt.
API_SALT = "_0x58fe15"
# The player page's main.js keys ``data-config`` with this salt.
PLAYER_CONFIG_SALT = "_0x59a0e4"


def is_javhdporn_url(url: str) -> bool:
    """Return whether ``url`` targets JavHDPorn."""

    host = (urlparse(url.strip()).hostname or "").lower()
    return host == "javhdporn.net" or host.endswith(".javhdporn.net")


def resolve_javhdporn_video_url(url: str, timeout: float = 30.0) -> str:
    """Resolve a JavHDPorn page URL to a direct media URL."""

    if not is_javhdporn_url(url):
        return url

    session = _new_session()
    page_html = _fetch_page(url, timeout=timeout, session=session)
    player_error: Exception | None = None
    if PLAYER_DIV_RE.search(page_html):
        try:
            return _resolve_player(page_html, url, timeout=timeout, session=session)
        except Exception as exc:  # noqa: BLE001 - fall back to direct MP4 links
            player_error = exc

    code = _code_from_url(url)
    media_url = _matching_media_url(page_html, code)
    if media_url:
        return media_url

    for candidate_url in _candidate_video_pages(page_html, url, code):
        candidate_html = _fetch_page(candidate_url, timeout=timeout, session=session)
        media_url = _matching_media_url(candidate_html, code)
        if media_url:
            return media_url

    detail = f" Player: {player_error}" if player_error else ""
    raise RuntimeError("Could not resolve a JavHDPorn media URL from the page." + detail)


def _resolve_player(page_html: str, page_url: str, timeout: float, session) -> str:
    token, version, video_id = play_request_from_page(page_html)
    origin = "{0.scheme}://{0.netloc}".format(urlparse(page_url))
    response = session.post(
        origin + PLAY_API_PATH,
        data={"sources": token, "ver": version},
        timeout=timeout,
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": origin,
            "Referer": page_url,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    response.raise_for_status()
    player_urls = player_urls_from_api(json.loads(response.text), video_id)
    if not player_urls:
        raise RuntimeError("the play API returned no servers")

    errors: list[str] = []
    for player_url in player_urls:
        if _is_media_url(player_url):
            return player_url
        try:
            player_html = _fetch_page(
                player_url, timeout=timeout, session=session, referer=page_url
            )
            media_url = media_url_from_player_page(player_html, player_url)
        except Exception as exc:  # noqa: BLE001 - try the next server
            errors.append(f"{player_url}: {exc}")
            continue
        if media_url:
            return media_url
        errors.append(f"{player_url}: no media URL")
    raise RuntimeError("; ".join(errors))


def play_request_from_page(html: str) -> tuple[str, str, str]:
    """Return ``(sources_token, data_ver, video_id)`` for the ``/api/play/`` POST."""

    div_match = PLAYER_DIV_RE.search(html)
    video_id_match = VIDEO_ID_RE.search(html)
    if not div_match or not video_id_match:
        raise RuntimeError("no video player on the page")
    div = div_match.group(0)
    mpu = _attribute(div, "data-mpu")
    version = _attribute(div, "data-ver") or "2"
    if not mpu:
        raise RuntimeError("the video player has no data-mpu token")
    video_id = video_id_match.group(1)

    salts = [MPU_SALTS.get(version), *MPU_SALTS.values(), API_SALT]
    for salt in dict.fromkeys(salt for salt in salts if salt):
        token = rc4_decrypt(_seed_key(video_id, salt), mpu)
        if token and re.fullmatch(r"[A-Za-z0-9_\-+/=]{16,}", token):
            return token, version, video_id
    raise RuntimeError("could not decrypt the data-mpu token")


def player_urls_from_api(payload: dict, video_id: str) -> tuple[str, ...]:
    """Decrypt the primary and ``reserve`` server URLs from a ``/api/play/`` answer."""

    if not payload.get("status"):
        raise RuntimeError(f"the play API refused the request: {payload}")
    blobs = [payload.get("data")]
    reserve = rc4_decrypt(_seed_key(video_id, API_SALT), payload.get("reserve") or "")
    if reserve:
        with contextlib.suppress(ValueError):
            blobs.extend(item.get("data") for item in json.loads(reserve) if isinstance(item, dict))

    urls: list[str] = []
    for blob in blobs:
        plain = rc4_decrypt(_seed_key(video_id, API_SALT), blob or "")
        if not plain:
            continue
        player_url = "https:" + plain if plain.startswith("//") else plain
        if re.match(r"https?://\S+$", player_url) and player_url not in urls:
            urls.append(player_url)
    return tuple(urls)


def media_url_from_player_page(html: str, player_url: str) -> str | None:
    """Decrypt the JW Player ``data-config`` of a ``/p/<id>`` page to its stream URL."""

    config_match = DATA_CONFIG_RE.search(html)
    if not config_match:
        return None
    path = urlparse(player_url).path
    seed = base64.b64encode(path.encode("utf-8")).decode("ascii")[4:]
    config_text = rc4_decrypt(_seed_key(seed, PLAYER_CONFIG_SALT), config_match.group(1))
    if not config_text:
        return None
    try:
        config = json.loads(config_text)
        sources = json.loads(_b64decode(str(config["src"])))
    except (ValueError, KeyError, TypeError):
        return None

    files = [str(source.get("file") or "") for source in sources if isinstance(source, dict)]
    files = [media_url.replace("\\/", "/") for media_url in files if media_url.startswith("http")]
    playlists = [media_url for media_url in files if ".m3u8" in media_url]
    return (playlists or files)[0] if playlists or files else None


def rc4_decrypt(key: bytes, blob: str) -> str | None:
    """``atob(rc4(key, atob(blob)))`` as done by the site JS; ``None`` on a wrong key."""

    try:
        ciphertext = _b64decode(blob)
        state = list(range(256))
        j = 0
        for i in range(256):
            j = (j + state[i] + key[i % len(key)]) % 256
            state[i], state[j] = state[j], state[i]
        i = j = 0
        plain = bytearray()
        for byte in ciphertext:
            i = (i + 1) % 256
            j = (j + state[i]) % 256
            state[i], state[j] = state[j], state[i]
            plain.append(byte ^ state[(state[i] + state[j]) % 256])
        return _b64decode(plain.decode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _seed_key(seed: str, salt: str) -> bytes:
    return base64.b64encode((seed + salt).encode("utf-8"))[::-1]


def _b64decode(value: str) -> bytes:
    value = value.strip().replace("\\/", "/")
    return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)


def _attribute(tag: str, name: str) -> str | None:
    match = re.search(rf"""\b{re.escape(name)}=["']([^"']*)["']""", tag)
    return unescape(match.group(1)) if match else None


def _is_media_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith((".m3u8", ".mp4"))


def _new_session():
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise RuntimeError("JavHDPorn support requires curl-cffi to be installed.") from exc

    return requests.Session(impersonate="chrome")


def _fetch_page(url: str, timeout: float, session, referer: str | None = None) -> str:
    headers = {"Accept-Language": "en-US,en;q=0.9"}
    if referer:
        headers["Referer"] = referer
    response = session.get(url, timeout=timeout, headers=headers)
    response.raise_for_status()
    return str(response.text)


def _matching_media_url(html: str, code: str | None) -> str | None:
    media_urls = _media_urls(html)
    if not media_urls:
        return None
    if code:
        normalized_code = _normalize_code(code)
        for media_url in media_urls:
            if normalized_code in _normalize_code(media_url):
                return media_url
        return None
    return media_urls[0] if len(media_urls) == 1 else None


def _media_urls(html: str) -> tuple[str, ...]:
    urls: list[str] = []
    for match in MEDIA_URL_RE.findall(html):
        media_url = _clean_url(match)
        if media_url not in urls and "/preview." not in media_url.lower():
            urls.append(media_url)
    return tuple(urls)


def _candidate_video_pages(html: str, current_url: str, code: str | None) -> tuple[str, ...]:
    current_path = _normalized_page_path(current_url)
    candidates: list[str] = []
    normalized_code = _normalize_code(code or "")
    for raw_url in VIDEO_PAGE_RE.findall(html):
        candidate = urljoin(current_url, _clean_url(raw_url))
        if "&" in urlparse(candidate).path:
            continue
        if _normalized_page_path(candidate) == current_path:
            continue
        if normalized_code and normalized_code not in _normalize_code(candidate):
            continue
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _code_from_url(url: str) -> str | None:
    path = urlparse(url).path
    match = SLUG_CODE_RE.search(path)
    if not match:
        return None
    return f"{match.group(1).lower()}-{int(match.group(2))}"


def _normalize_code(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _clean_url(url: str) -> str:
    return unescape(url).replace("\\/", "/").rstrip(".,")


def _normalized_page_path(url: str) -> str:
    clean_url = urldefrag(url)[0]
    parsed = urlparse(clean_url)
    return parsed.path.rstrip("/")
