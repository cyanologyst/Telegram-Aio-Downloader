from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import yt_dlp

MODEL_PATH_RE = re.compile(r"^/model/(?P<slug>[A-Za-z0-9_-]+)/?$", re.I)


@dataclass(frozen=True, slots=True)
class PornHubModelPlaylist:
    title: str
    site: str
    source_url: str
    slug: str
    urls: tuple[str, ...]


def is_pornhub_model_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    return _is_pornhub_host(host) and bool(MODEL_PATH_RE.fullmatch(parsed.path.rstrip("/") + "/"))


async def resolve_pornhub_model_playlist(
    url: str,
    *,
    cookies_file: str | None = None,
    proxy: str | None = None,
) -> PornHubModelPlaylist:
    return await asyncio.to_thread(
        _resolve_pornhub_model_playlist_sync,
        url,
        cookies_file,
        proxy,
    )


def _resolve_pornhub_model_playlist_sync(
    url: str,
    cookies_file: str | None = None,
    proxy: str | None = None,
) -> PornHubModelPlaylist:
    if not is_pornhub_model_url(url):
        raise ValueError("Unsupported PornHub model URL")

    opts: dict[str, object] = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 45,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
    }
    if cookies_file and Path(cookies_file).exists():
        opts["cookiefile"] = cookies_file
    if proxy:
        opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = tuple(
        video_url
        for video_url in (_entry_url(url, entry) for entry in (info or {}).get("entries") or [])
        if video_url
    )
    if not entries:
        raise RuntimeError("No public videos found on this PornHub model page.")

    slug = _model_slug(url)
    title = str((info or {}).get("title") or f"PornHub model {slug}")
    return PornHubModelPlaylist(
        title=title,
        site="PornHub",
        source_url=url,
        slug=slug,
        urls=entries,
    )


def _entry_url(base_url: str, entry: object) -> str | None:
    if not isinstance(entry, dict):
        return None
    raw_url = str(entry.get("url") or "").strip()
    if not raw_url:
        return None
    video_url = urljoin(base_url, raw_url)
    parsed = urlparse(video_url)
    if not _is_pornhub_host(parsed.hostname or ""):
        return None
    if "view_video.php" not in parsed.path:
        return None
    return video_url


def _model_slug(url: str) -> str:
    match = MODEL_PATH_RE.fullmatch(urlparse(url.strip()).path.rstrip("/") + "/")
    return match.group("slug") if match else "model"


def _is_pornhub_host(host: str) -> bool:
    clean_host = host.lower().removeprefix("www.")
    return clean_host == "pornhub.com" or clean_host.endswith(".pornhub.com")
