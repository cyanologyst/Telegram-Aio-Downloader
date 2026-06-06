from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

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
        return bool(
            re.fullmatch(r"video/[^/]+/?", path)
            or re.fullmatch(r"studio/[^/]+(?:/page/\d+)?/?", path)
        )

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


def _hentaihaven_episode_urls(page: str, slug: str | None = None) -> set[str]:
    slug_part = re.escape(slug) if slug else r"[^/]+"
    return {
        f"https://hentaihaven.com/video/{match.group(1)}/episode-{match.group(2)}"
        for match in re.finditer(rf"/video/({slug_part})/episode-(\d+)", page)
    }


def _hentaihaven_series_urls(page: str, base_url: str) -> set[str]:
    urls = set()
    for match in re.finditer(r"""href=["']([^"']*/video/[a-z0-9-]+/?)["']""", page, re.I):
        href = match.group(1)
        if "/episode-" in href:
            continue
        urls.add(urljoin(base_url, href).rstrip("/"))
    return urls


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
        video_match = re.fullmatch(r"video/([^/]+)", path)
        if video_match:
            slug = video_match.group(1)
            episode_urls = _hentaihaven_episode_urls(page, slug)
            return HentaiPlaylist(
                title=_clean_title(page, slug.replace("-", " ").title()),
                site="HentaiHaven",
                urls=_unique_sorted_episode_urls(episode_urls),
            )

        studio_match = re.fullmatch(r"studio/([^/]+)(?:/page/\d+)?", path)
        if not studio_match:
            raise ValueError("Unsupported HentaiHaven playlist URL")

        episode_urls = _hentaihaven_episode_urls(page)
        series_urls = _hentaihaven_series_urls(page, url)
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=request_timeout,
        ) as client:
            for series_url in sorted(series_urls):
                series_response = await client.get(series_url)
                series_response.raise_for_status()
                episode_urls.update(_hentaihaven_episode_urls(series_response.text))

        slug = studio_match.group(1)
        return HentaiPlaylist(
            title=_clean_title(page, f"{slug.replace('-', ' ').title()} Studio"),
            site="HentaiHaven Studio",
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
