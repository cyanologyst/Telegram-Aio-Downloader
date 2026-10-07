"""One interface over the torrent search backends (Prowlarr, TPB, RARBG-style).

The Telegram UI (app/bot/search.py) only talks to ``SearchProvider``; each
adapter wraps the existing crawler/client and normalises its results into
``SearchResult`` dictionaries that can live in ``context.user_data``.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.downloaders.torrents.prowlarr.client import ProwlarrClient
from app.downloaders.torrents.rarbg.crawler import RARBGCrawler
from app.downloaders.torrents.tpb.crawler import TPBCrawler

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SearchResult:
    title: str
    size: str = "?"
    seeders: int | None = None
    leechers: int | None = None
    source: str = ""  # indexer or category shown under the title
    added: str = ""
    magnet: str = ""  # known up front for TPB results
    can_select_files: bool = False  # a .torrent may be available (Prowlarr)
    url: str = ""  # the result's page on the site, linked from its title
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ResolvedSource:
    """What to hand to aria2: a magnet/URL, and the .torrent path if one was saved."""

    source: str
    torrent_path: Path | None = None


class SearchProvider(Protocol):
    key: str
    label: str
    description: str
    categories: dict[str, str]  # category key -> button label, "all" first

    @property
    def enabled(self) -> bool: ...

    async def search_page(self, query: str, category: str, page: int) -> list[SearchResult]:
        """One backend page of results; an empty list means there are no more."""

    @property
    def paginates(self) -> bool:
        """True when later backend pages can return more results."""

    async def resolve(self, result: dict[str, Any], torrent_dir: Path) -> ResolvedSource: ...


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _web_url(value: Any) -> str:
    url = str(value or "")
    return url if url.startswith(("http://", "https://")) else ""


def _tpb_date(value: Any) -> str:
    """apibay gives a Unix timestamp; show it as a date."""
    stamp = _to_int(value)
    if not stamp:
        return ""
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%d")


def _human_bytes(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class ProwlarrProvider:
    key = "prowlarr"
    label = "🧭 Prowlarr"
    description = "all your Prowlarr indexers"

    def __init__(self, client: ProwlarrClient):
        self.client = client
        self.categories = dict(ProwlarrClient.CATEGORY_LABELS)

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    @property
    def paginates(self) -> bool:
        return False

    async def search_page(self, query: str, category: str, page: int) -> list[SearchResult]:
        if page > 0:
            return []
        releases = await self.client.search(query, category or "all")
        return [
            SearchResult(
                title=str(item.get("title") or "Unknown release"),
                size=_human_bytes(int(item.get("size") or 0)),
                seeders=item.get("seeders"),
                leechers=item.get("leechers"),
                source=str(item.get("indexer") or ""),
                added=str(item.get("publish_date") or "")[:10],
                magnet=(
                    str(item.get("magnet_url") or "")
                    if str(item.get("magnet_url") or "").startswith("magnet:")
                    else ""
                ),
                can_select_files=not str(item.get("magnet_url") or "").startswith("magnet:"),
                url=_web_url(item.get("info_url")),
                raw=item,
            )
            for item in releases
        ]

    async def resolve(self, result: dict[str, Any], torrent_dir: Path) -> ResolvedSource:
        source, torrent_path = await self.client.resolve_download_source(result["raw"], torrent_dir)
        return ResolvedSource(source=source, torrent_path=torrent_path)


class TPBProvider:
    key = "tpb"
    label = "🏴‍☠️ The Pirate Bay"
    description = "public apibay.org index"

    def __init__(self, crawler: TPBCrawler):
        self.crawler = crawler
        self.categories = dict(TPBCrawler.CATEGORY_LABELS)

    @property
    def enabled(self) -> bool:
        return bool(self.crawler.api_url)

    @property
    def paginates(self) -> bool:
        return False

    async def search_page(self, query: str, category: str, page: int) -> list[SearchResult]:
        if page > 0:
            return []
        code = TPBCrawler.CATEGORIES.get(category or "all", "0")
        items = await self.crawler.search(query, category=code)
        results = []
        for item in items:
            # apibay answers "no results" with a single placeholder row of id 0.
            if str(item.get("id", "0")) == "0":
                continue
            info_hash = str(item.get("info_hash") or "")
            name = str(item.get("name") or "Unknown")
            results.append(
                SearchResult(
                    title=name,
                    size=TPBCrawler.human_size(item.get("size", "0")),
                    seeders=_to_int(item.get("seeders")),
                    leechers=_to_int(item.get("leechers")),
                    source=str(item.get("username") or ""),
                    magnet=TPBCrawler.build_magnet(info_hash, name) if info_hash else "",
                    added=_tpb_date(item.get("added")),
                    url=f"https://thepiratebay.org/description.php?id={item.get('id')}",
                    raw={"id": str(item.get("id"))},
                )
            )
        return results

    async def resolve(self, result: dict[str, Any], torrent_dir: Path) -> ResolvedSource:
        if result.get("magnet"):
            return ResolvedSource(source=result["magnet"])
        item = await self.crawler.get_torrent_details(result["raw"]["id"])
        if not item or not item.get("info_hash"):
            raise RuntimeError("The Pirate Bay did not return a magnet for this result.")
        return ResolvedSource(
            source=TPBCrawler.build_magnet(item["info_hash"], item.get("name", result["title"]))
        )


class RARBGProvider:
    key = "rarbg"
    label = "🧲 RARBG mirror"
    description = "RARBG-style mirror site"

    def __init__(self, crawler: RARBGCrawler):
        self.crawler = crawler
        self.categories = dict(RARBGCrawler.CATEGORY_LABELS)

    @property
    def enabled(self) -> bool:
        return bool(self.crawler.base_url)

    @property
    def paginates(self) -> bool:
        return True

    async def search_page(self, query: str, category: str, page: int) -> list[SearchResult]:
        code = RARBGCrawler.CATEGORIES.get(category or "all", "")
        items = await self.crawler.search(query, category=code, page=page)
        return [
            SearchResult(
                title=str(item.get("name") or "Unknown"),
                size=str(item.get("size") or "?"),
                seeders=_to_int(item.get("seeders")),
                leechers=_to_int(item.get("leechers")),
                source=str(item.get("category") or ""),
                added=str(item.get("added") or ""),
                magnet=str(item.get("magnet") or ""),
                url=_web_url(item.get("url")),
                raw={"id": str(item.get("id") or "")},
            )
            for item in items
            if item.get("id")
        ]

    async def resolve(self, result: dict[str, Any], torrent_dir: Path) -> ResolvedSource:
        if result.get("magnet"):
            return ResolvedSource(source=result["magnet"])
        item = await self.crawler.get_torrent_details(result["raw"]["id"])
        magnet = (item or {}).get("magnet", "")
        if not magnet:
            raise RuntimeError("The mirror did not return a magnet for this result.")
        return ResolvedSource(source=magnet)


def _identity(item: dict[str, Any]) -> tuple[str, ...]:
    raw = item.get("raw") or {}
    backend_id = str(raw.get("id") or raw.get("token") or "")
    if backend_id:
        return ("id", backend_id)
    return ("title", item["title"], item.get("magnet", ""))


async def fetch_until(
    provider: SearchProvider,
    session: dict[str, Any],
    wanted: int,
) -> None:
    """Grow ``session["items"]`` from backend pages until ``wanted`` items or exhausted.

    Results are de-duplicated (by backend id when there is one) so
    overlapping backend pages do not repeat rows.
    """
    seen = {_identity(item) for item in session["items"]}
    while len(session["items"]) < wanted and not session["exhausted"]:
        batch = await provider.search_page(
            session["query"], session["category"], session["next_page"]
        )
        session["next_page"] += 1
        fresh = []
        for result in batch:
            item = result.to_dict()
            if _identity(item) not in seen:
                seen.add(_identity(item))
                fresh.append(item)
        session["items"].extend(fresh)
        if not fresh or not provider.paginates:
            session["exhausted"] = True
