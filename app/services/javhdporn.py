"""JavHDPorn page resolver.

JavHDPorn pages are Cloudflare-protected and hide the playable URL behind a
secondary video page. The bot resolves those public pages to a direct media URL
first, then lets the existing yt-dlp workflow download the file.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urldefrag, urljoin, urlparse

MEDIA_URL_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:mp4|m3u8)(?:\?[^\s\"'<>]*)?", re.I)
VIDEO_PAGE_RE = re.compile(r"https?://(?:www\.)?javhdporn\.net/(?:v\d+/)?video/[^\"'<> ]+/?", re.I)
SLUG_CODE_RE = re.compile(r"([a-z]{2,10})[-_]?0*(\d{2,6})", re.I)


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
    code = _code_from_url(url)
    media_url = _matching_media_url(page_html, code)
    if media_url:
        return media_url

    for candidate_url in _candidate_video_pages(page_html, url, code):
        candidate_html = _fetch_page(candidate_url, timeout=timeout, session=session)
        media_url = _matching_media_url(candidate_html, code)
        if media_url:
            return media_url

    raise RuntimeError("Could not resolve a JavHDPorn media URL from the page.")


def _new_session():
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise RuntimeError("JavHDPorn support requires curl-cffi to be installed.") from exc

    return requests.Session(impersonate="chrome")


def _fetch_page(url: str, timeout: float, session) -> str:
    response = session.get(
        url,
        timeout=timeout,
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )
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
