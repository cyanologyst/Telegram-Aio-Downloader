"""Small async Prowlarr API client."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx


class ProwlarrConfigError(RuntimeError):
    """Raised when Prowlarr is not configured."""


@dataclass(slots=True)
class ProwlarrRelease:
    """Normalized Prowlarr release search result."""

    token: str
    title: str
    indexer: str
    size: int
    seeders: int | None
    leechers: int | None
    publish_date: str
    download_url: str
    magnet_url: str
    info_url: str
    protocol: str
    categories: list[str]

    @property
    def source_url(self) -> str:
        return self.magnet_url or self.download_url

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "title": self.title,
            "indexer": self.indexer,
            "size": self.size,
            "seeders": self.seeders,
            "leechers": self.leechers,
            "publish_date": self.publish_date,
            "download_url": self.download_url,
            "magnet_url": self.magnet_url,
            "info_url": self.info_url,
            "protocol": self.protocol,
            "categories": self.categories,
            "source_url": self.source_url,
        }


class ProwlarrClient:
    """Search Prowlarr and resolve result download URLs."""

    CATEGORY_PRESETS = {
        "all": [],
        "movies": [2000],
        "tv": [5000],
        "anime": [5070],
        "music": [3000],
        "apps": [4000],
        "books": [8000],
        "xxx": [6000],
    }

    CATEGORY_LABELS = {
        "all": "🌐 All",
        "movies": "🎬 Movies",
        "tv": "📺 TV",
        "anime": "🎞 Anime",
        "music": "🎵 Music",
        "apps": "🖥 Apps",
        "books": "📚 Books",
        "xxx": "🔞 XXX",
    }

    def __init__(self, base_url: str, api_key: str, limit: int = 20):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.limit = max(1, min(int(limit or 20), 100))
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=45.0,
                follow_redirects=False,
                headers={"X-Api-Key": self.api_key},
            )
        return self._client

    def _require_config(self) -> None:
        if not self.enabled:
            raise ProwlarrConfigError(
                "Prowlarr is not configured. Set PROWLARR_URL and PROWLARR_API_KEY in .env."
            )

    async def search(self, query: str, category: str = "all") -> list[dict[str, Any]]:
        """Run an interactive Prowlarr search."""
        self._require_config()
        categories = self.CATEGORY_PRESETS.get(category, [])
        params: dict[str, str | int] = {
            "query": query,
            "type": "search",
            "limit": self.limit,
        }
        if categories:
            params["categories"] = ",".join(str(item) for item in categories)

        response = await self.client.get(
            urljoin(self.base_url + "/", "api/v1/search"),
            params=params,
        )
        response.raise_for_status()
        releases = response.json()
        if not isinstance(releases, list):
            return []

        return [
            self._normalize_release(item, str(index)).to_dict()
            for index, item in enumerate(releases[: self.limit])
            if isinstance(item, dict)
        ]

    async def resolve_download_source(
        self,
        release: dict[str, Any],
        destination_dir: Path,
    ) -> tuple[str, Path | None]:
        """Return a magnet URL or saved torrent path for a Prowlarr release."""
        magnet = str(release.get("magnet_url") or "")
        if magnet.startswith("magnet:"):
            return magnet, None

        url = str(release.get("download_url") or release.get("source_url") or "")
        if not url:
            raise RuntimeError("This Prowlarr result does not include a download URL.")
        if url.startswith("/"):
            url = urljoin(self.base_url + "/", url.lstrip("/"))

        response = await self.client.get(url)
        if response.is_redirect:
            location = response.headers.get("Location", "")
            if location.startswith("magnet:"):
                return location, None
            if location:
                response = await self.client.get(location)

        content_type = response.headers.get("Content-Type", "").lower()
        text_preview = response.text[:200] if "text" in content_type else ""
        if response.url and str(response.url).startswith("magnet:"):
            return str(response.url), None
        if text_preview.strip().startswith("magnet:"):
            return text_preview.strip(), None

        response.raise_for_status()
        await _mkdir(destination_dir)
        title = sanitize_torrent_filename(str(release.get("title") or "prowlarr-result"))
        torrent_path = destination_dir / f"{title}.torrent"
        await _write_bytes(torrent_path, response.content)
        return str(torrent_path), torrent_path

    @staticmethod
    def _normalize_release(item: dict[str, Any], token: str) -> ProwlarrRelease:
        categories = []
        for category in item.get("categories") or []:
            if isinstance(category, dict):
                categories.append(str(category.get("name") or category.get("id") or "").strip())
            else:
                categories.append(str(category).strip())
        return ProwlarrRelease(
            token=token,
            title=str(item.get("title") or "Unknown release"),
            indexer=str(item.get("indexer") or item.get("indexerId") or "Unknown"),
            size=int(item.get("size") or 0),
            seeders=_optional_int(item.get("seeders")),
            leechers=_optional_int(item.get("leechers")),
            publish_date=str(item.get("publishDate") or ""),
            download_url=str(item.get("downloadUrl") or ""),
            magnet_url=str(item.get("magnetUrl") or ""),
            info_url=str(item.get("infoUrl") or ""),
            protocol=str(item.get("protocol") or "torrent"),
            categories=[item for item in categories if item],
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _write_bytes(path: Path, content: bytes) -> None:
    def write() -> None:
        path.write_bytes(content)

    import asyncio

    await asyncio.to_thread(write)


async def _mkdir(path: Path) -> None:
    import asyncio

    await asyncio.to_thread(path.mkdir, parents=True, exist_ok=True)


def sanitize_torrent_filename(name: str) -> str:
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(". ")
    clean = re.sub(r"\s+", " ", clean).strip()
    return (clean or "prowlarr-result")[:160]
