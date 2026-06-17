"""Resolvers for adult video sites that need page pre-processing."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from urllib.parse import urlparse

from app.services.javhdporn import is_javhdporn_url, resolve_javhdporn_video_url

PACKED_JS_RE = re.compile(
    r"eval\(function\(p,a,c,k,e,d\).*?\(\s*"
    r"'(?P<payload>(?:\\'|[^'])*)'\s*,\s*"
    r"(?P<radix>\d+)\s*,\s*"
    r"(?P<count>\d+)\s*,\s*"
    r"'(?P<symbols>(?:\\'|[^'])*)'\.split\('\|'\)",
    re.S,
)
MEDIA_URL_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:mp4|m3u8)(?:\?[^\s\"'<>]*)?", re.I)
JAVTIFUL_CONFIG_RE = re.compile(
    r'<script[^>]+id=["\']frontWatchConfig["\'][^>]*>(?P<json>.*?)</script>',
    re.I | re.S,
)
MISSAV_LIKE_DOMAINS = frozenset(
    {
        "missav.com",
        "missav.live",
        "missav.ws",
        "missav123.com",
        "njavtv.com",
    }
)
GENERIC_RESOLVED_DOMAINS = frozenset(
    {
        "alphaporno.com",
        "camsoda.com",
        "nonktube.com",
        "sexu.com",
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedAdultVideo:
    """URL and headers needed by yt-dlp for a resolved adult video page."""

    url: str
    referer: str | None = None


def resolve_adult_video_url(url: str, timeout: float = 30.0) -> ResolvedAdultVideo:
    """Resolve adult video page URLs that yt-dlp cannot handle directly."""

    if is_javhdporn_url(url):
        return ResolvedAdultVideo(resolve_javhdporn_video_url(url, timeout=timeout), referer=url)
    if _is_missav_like_url(url):
        return ResolvedAdultVideo(_resolve_missav_like_url(url, timeout=timeout), referer=url)
    if _is_javtiful_url(url):
        return ResolvedAdultVideo(_resolve_javtiful_url(url, timeout=timeout), referer=url)
    if _is_generic_resolved_url(url):
        return ResolvedAdultVideo(
            _resolve_generic_adult_media_url(url, timeout=timeout), referer=url
        )
    return ResolvedAdultVideo(url)


def resolved_video_output_template(output_dir: Path, original_url: str) -> str:
    """Build a short yt-dlp output template for pre-resolved signed media URLs."""

    url_token = hashlib.sha1(original_url.encode("utf-8")).hexdigest()[:12]
    return str(output_dir / f"%(title).160B [{url_token}].%(ext)s")


def _resolve_missav_like_url(url: str, timeout: float) -> str:
    html = _fetch_page(url, timeout=timeout)
    for decoded in _decode_packed_javascript(html):
        media_urls = _media_urls(decoded)
        if media_urls:
            playlist = next(
                (media_url for media_url in media_urls if "playlist.m3u8" in media_url), None
            )
            return playlist or media_urls[0]
    raise RuntimeError("Could not resolve a media URL from this MissAV/NJAV page.")


def _resolve_javtiful_url(url: str, timeout: float) -> str:
    html = _fetch_page(url, timeout=timeout)
    media_url = _front_watch_config_media_url(html)
    if media_url:
        return media_url

    media_urls = [
        media_url
        for media_url in _media_urls(html)
        if "/previews/" not in media_url.lower() and "_preview." not in media_url.lower()
    ]
    if media_urls:
        return media_urls[0]
    raise RuntimeError("Could not resolve a media URL from this Javtiful page.")


def _resolve_generic_adult_media_url(url: str, timeout: float) -> str:
    html = _fetch_page(url, timeout=timeout)
    media_url = _front_watch_config_media_url(html)
    if media_url:
        return media_url

    for decoded in _decode_packed_javascript(html):
        media_urls = _media_urls(decoded)
        if media_urls:
            playlist = next(
                (media_url for media_url in media_urls if "playlist.m3u8" in media_url), None
            )
            return playlist or media_urls[0]

    filtered_media_urls = [
        media_url
        for media_url in _media_urls(html)
        if "/previews/" not in media_url.lower() and "_preview." not in media_url.lower()
    ]
    if filtered_media_urls:
        playlist = next(
            (media_url for media_url in filtered_media_urls if ".m3u8" in media_url), None
        )
        return playlist or filtered_media_urls[0]
    raise RuntimeError("Could not resolve a media URL from this adult video page.")


def _front_watch_config_media_url(html: str) -> str | None:
    config_match = JAVTIFUL_CONFIG_RE.search(html)
    if config_match:
        config = json.loads(unescape(config_match.group("json")))
        sources = sorted(
            config.get("playerSources", []),
            key=_source_size,
            reverse=True,
        )
        for source in sources:
            media_url = str(source.get("src") or "")
            if media_url:
                return media_url
    return None


def _fetch_page(url: str, timeout: float) -> str:
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise RuntimeError("This site requires curl-cffi to be installed.") from exc

    response = requests.get(
        url,
        impersonate="chrome",
        timeout=timeout,
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    response.raise_for_status()
    return str(response.text)


def _decode_packed_javascript(html: str) -> tuple[str, ...]:
    decoded: list[str] = []
    for match in PACKED_JS_RE.finditer(html):
        try:
            payload = ast.literal_eval(f"'{match.group('payload')}'")
            symbols = tuple(ast.literal_eval(f"'{match.group('symbols')}'").split("|"))
        except (SyntaxError, ValueError):
            continue
        radix = int(match.group("radix"))

        unpacked = _unpack_packed_payload(payload, radix, symbols)
        if ".m3u8" in unpacked or ".mp4" in unpacked:
            decoded.append(unpacked)
    return tuple(decoded)


def _unpack_packed_payload(payload: str, radix: int, symbols: tuple[str, ...]) -> str:
    def replace_word(word_match: re.Match[str]) -> str:
        word = word_match.group(0)
        index = _base_to_int(word, radix)
        if index is None or index >= len(symbols) or not symbols[index]:
            return word
        return symbols[index]

    return re.sub(r"\b\w+\b", replace_word, payload)


def _source_size(source: dict[str, object]) -> int:
    size = source.get("size")
    if isinstance(size, int):
        return size
    match = re.search(r"\d+", str(size or ""))
    return int(match.group(0)) if match else 0


def _media_urls(text: str) -> tuple[str, ...]:
    normalized_text = text.replace("\\/", "/")
    urls: list[str] = []
    for match in MEDIA_URL_RE.findall(normalized_text):
        media_url = unescape(match).replace("\\u0026", "&")
        if media_url not in urls:
            urls.append(media_url)
    return tuple(urls)


def _base_to_int(value: str, radix: int) -> int | None:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    number = 0
    for char in value.lower():
        digit = alphabet.find(char)
        if digit < 0 or digit >= radix:
            return None
        number = number * radix + digit
    return number


def _is_missav_like_url(url: str) -> bool:
    host = _host(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in MISSAV_LIKE_DOMAINS)


def _is_javtiful_url(url: str) -> bool:
    host = _host(url)
    return host == "javtiful.com" or host.endswith(".javtiful.com")


def _is_generic_resolved_url(url: str) -> bool:
    host = _host(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in GENERIC_RESOLVED_DOMAINS)


def _host(url: str) -> str:
    return (urlparse(url.strip()).hostname or "").lower()
