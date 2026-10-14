"""MissAV page resolver.

MissAV ships the HLS playlist (``surrit.com/<uuid>/playlist.m3u8``) inside a
packed ``eval(function(p,a,c,k,e,d)...)`` script on the video page. The main
domains (missav.ws, missav.ai) sit behind a Cloudflare challenge and
missav.com/thisav.com redirect to a landing page, so the same path is retried
on mirrors that serve the real page. The playlist only loads with a MissAV
``Referer``, so the resolver returns the mirror page it read alongside the URL.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

from app.services.packed_js import decode_packed_javascript

MISSAV_DOMAINS = (
    "missav.ws",
    "missav.ai",
    "missav.com",
    "missav.io",
    "missav.live",
    "missav123.com",
    "missav01.com",
    "njavtv.com",
)
# Mirrors that answer a plain browser-impersonating request with the video page.
MIRROR_HOSTS = ("missav.live", "missav123.com", "missav01.com", "njavtv.com")
MEDIA_URL_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:m3u8|mp4)(?:\?[^\s\"'<>]*)?", re.I)
SURRIT_PLAYLIST_RE = re.compile(
    r"https?://surrit\.com/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/playlist\.m3u8"
)


def is_missav_url(url: str) -> bool:
    """Return whether ``url`` targets MissAV or one of its mirrors."""

    host = _host(url)
    return any(host == domain or host.endswith(f".{domain}") for domain in MISSAV_DOMAINS)


def resolve_missav_video(url: str, timeout: float = 30.0) -> tuple[str, str]:
    """Resolve a MissAV video page to ``(media_url, referer_page_url)``."""

    session = _new_session()
    errors: list[str] = []
    for page_url in candidate_page_urls(url):
        host = _host(page_url)
        try:
            html, final_url = _fetch_page(page_url, timeout=timeout, session=session)
        except Exception as exc:  # noqa: BLE001 - try the next mirror
            errors.append(f"{host}: {exc}")
            continue
        if urlparse(final_url).path.strip("/") == "":
            errors.append(f"{host}: redirected to the homepage")
            continue
        media_url = media_url_from_page(html)
        if media_url:
            return media_url, final_url
        errors.append(f"{host}: no player on the page")
    raise RuntimeError("Could not resolve a MissAV media URL. " + "; ".join(errors))


def candidate_page_urls(url: str) -> tuple[str, ...]:
    """The given page first, then the same path on every working mirror."""

    parsed = urlparse(url.strip())
    own_host = _host(url)
    urls = [urlunparse(parsed)]
    for mirror in MIRROR_HOSTS:
        if own_host == mirror or own_host.endswith(f".{mirror}"):
            continue
        urls.append(urlunparse(parsed._replace(scheme="https", netloc=mirror)))
    return tuple(urls)


def media_url_from_page(html: str) -> str | None:
    """Pull the HLS playlist out of a MissAV page, preferring the master playlist."""

    for decoded in decode_packed_javascript(html):
        media_urls = MEDIA_URL_RE.findall(decoded.replace("\\/", "/"))
        if media_urls:
            playlists = [media_url for media_url in media_urls if "playlist.m3u8" in media_url]
            return (playlists or media_urls)[0]
    match = SURRIT_PLAYLIST_RE.search(html.replace("\\/", "/"))
    return match.group(0) if match else None


def _host(url: str) -> str:
    host = (urlparse(url.strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _new_session():
    try:
        from curl_cffi import requests
    except ImportError as exc:
        raise RuntimeError("MissAV support requires curl-cffi to be installed.") from exc

    return requests.Session(impersonate="chrome")


def _fetch_page(url: str, timeout: float, session) -> tuple[str, str]:
    response = session.get(url, timeout=timeout, headers={"Accept-Language": "en-US,en;q=0.9"})
    response.raise_for_status()
    return str(response.text), str(getattr(response, "url", "") or url)
