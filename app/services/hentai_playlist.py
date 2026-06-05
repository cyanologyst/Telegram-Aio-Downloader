from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True)
class HentaiPlaylist:
    title: str
    site: str
    urls: list[str]


def is_hentai_playlist_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path.strip("/")

    if host.endswith("hentaihaven.com"):
        return bool(re.fullmatch(r"video/[^/]+/?", path))

    if host.endswith("hstream.moe"):
        match = re.fullmatch(r"hentai/([a-z0-9-]+)/?", path)
        return bool(match and not re.search(r"-\d+$", match.group(1)))

    return False


def _clean_title(page: str, fallback: str) -> str:
    match = re.search(r"<title>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    if not match:
        return fallback
    title = re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()
    title = re.sub(r"\s*[-|]\s*(Hentai Haven|hstream\.moe).*$", "", title, flags=re.I)
    return title or fallback


def _unique_sorted_episode_urls(urls: set[str]) -> list[str]:
    def episode_number(item: str) -> tuple[int, str]:
        match = re.search(r"(?:episode-|-(\d+)$)(\d+)?", item.rstrip("/"))
        if not match:
            return (999999, item)
        value = match.group(2) or match.group(1) or "999999"
        return (int(value), item)

    return sorted(urls, key=episode_number)


async def resolve_hentai_playlist(url: str, request_timeout: float = 20.0) -> HentaiPlaylist:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    path = parsed.path.strip("/")
    headers = {"User-Agent": "Mozilla/5.0"}

    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=request_timeout,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        page = response.text

    if host.endswith("hentaihaven.com"):
        match = re.fullmatch(r"video/([^/]+)", path)
        if not match:
            raise ValueError("Unsupported HentaiHaven playlist URL")
        slug = match.group(1)
        episode_urls = {
            f"https://hentaihaven.com/video/{slug}/episode-{number}"
            for number in re.findall(rf"/video/{re.escape(slug)}/episode-(\d+)", page)
        }
        return HentaiPlaylist(
            title=_clean_title(page, slug.replace("-", " ").title()),
            site="HentaiHaven",
            urls=_unique_sorted_episode_urls(episode_urls),
        )

    if host.endswith("hstream.moe"):
        match = re.fullmatch(r"hentai/([a-z0-9-]+)", path)
        if not match:
            raise ValueError("Unsupported HStream playlist URL")
        slug = match.group(1)
        episode_urls = {
            f"https://hstream.moe/hentai/{slug}-{number}"
            for number in re.findall(rf"/hentai/{re.escape(slug)}-(\d+)", page)
        }
        return HentaiPlaylist(
            title=_clean_title(page, slug.replace("-", " ").title()),
            site="HStream",
            urls=_unique_sorted_episode_urls(episode_urls),
        )

    raise ValueError("Unsupported hentai playlist site")
