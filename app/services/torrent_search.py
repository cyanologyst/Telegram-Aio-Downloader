"""One interface over the torrent search backends (Prowlarr, TPB, RARBG-style).

The Telegram UI (app/bot/search.py) only talks to ``SearchProvider``; each
adapter wraps the existing crawler/client and normalises its results into
``SearchResult`` dictionaries that can live in ``context.user_data``.
"""

from __future__ import annotations

import html
import logging
import re
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


# Result sorting: (criterion, direction) stored as "seeders:desc". Picking a
# criterion starts at its natural direction (most seeders, biggest, newest, A-Z).
SORT_FIELDS: dict[str, tuple[str, str]] = {
    "seeders": ("🟢 Seeders", "desc"),
    "size": ("💾 Size", "desc"),
    "date": ("📅 Date", "desc"),
    "name": ("🔤 Name", "asc"),
}
DEFAULT_SORT = "seeders:desc"
# What each direction means for a criterion, for the direction button.
SORT_DIRECTIONS: dict[str, dict[str, str]] = {
    "seeders": {"desc": "↓ Most", "asc": "↑ Fewest"},
    "size": {"desc": "↓ Largest", "asc": "↑ Smallest"},
    "date": {"desc": "↓ Newest", "asc": "↑ Oldest"},
    "name": {"asc": "↑ A–Z", "desc": "↓ Z–A"},
}

_SIZE_RE = re.compile(r"([\d.,]+)\s*([KMGTP]?)(i?)B", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?")


def parse_sort(value: Any) -> tuple[str, str]:
    """``"size:asc"`` -> ("size", "asc"); anything unknown -> the default."""
    field_name, _, direction = str(value or "").partition(":")
    if field_name not in SORT_FIELDS:
        field_name, _, direction = DEFAULT_SORT.partition(":")
    if direction not in ("asc", "desc"):
        direction = SORT_FIELDS[field_name][1]
    return field_name, direction


def next_sort_field(value: Any) -> str:
    """The next criterion, at its natural direction."""
    names = list(SORT_FIELDS)
    current, _ = parse_sort(value)
    following = names[(names.index(current) + 1) % len(names)]
    return f"{following}:{SORT_FIELDS[following][1]}"


def flip_sort_direction(value: Any) -> str:
    field_name, direction = parse_sort(value)
    return f"{field_name}:{'asc' if direction == 'desc' else 'desc'}"


def sort_label(value: Any) -> str:
    """``"size:asc"`` -> "💾 Size ↑"."""
    field_name, direction = parse_sort(value)
    return f"{SORT_FIELDS[field_name][0]} {'↓' if direction == 'desc' else '↑'}"


def size_bytes(text: Any) -> int | None:
    """``"1.4 GB"`` / ``"700 MiB"`` -> bytes; None when it can't be read."""
    match = _SIZE_RE.search(str(text or ""))
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = match.group(2).upper()
    power = "KMGTP".index(unit) + 1 if unit else 0
    return int(number * 1024**power)


def _date_key(text: Any) -> tuple[int, ...] | None:
    match = _DATE_RE.search(str(text or ""))
    return tuple(int(part or 0) for part in match.groups()) if match else None


def _sort_value(item: dict[str, Any], field_name: str) -> Any:
    if field_name == "seeders":
        return _to_int(item.get("seeders"))
    if field_name == "size":
        return size_bytes(item.get("size"))
    if field_name == "date":
        return _date_key(item.get("added"))
    title = html.unescape(str(item.get("title") or "")).strip().casefold()
    return title or None


def sort_items(items: list[dict[str, Any]], value: Any) -> None:
    """Sort result dicts in place; results missing the value go last either way."""
    field_name, direction = parse_sort(value)
    known = [(v, item) for item in items if (v := _sort_value(item, field_name)) is not None]
    unknown = [item for item in items if _sort_value(item, field_name) is None]
    known.sort(key=lambda pair: pair[0], reverse=direction == "desc")
    items[:] = [item for _, item in known] + unknown


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
