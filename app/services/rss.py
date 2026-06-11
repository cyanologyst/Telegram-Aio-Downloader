"""RSS/Atom feed discovery for automated download workflows."""

from __future__ import annotations

import email.utils
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx


@dataclass(frozen=True, slots=True)
class FeedEntry:
    """Download candidate discovered from an RSS or Atom feed."""

    id: str
    title: str
    url: str
    published_at: datetime | None = None


class FeedReader:
    """Fetch and parse RSS/Atom feeds without adding a heavy dependency."""

    def __init__(self, timeout_seconds: float = 20.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def fetch(self, feed_url: str) -> tuple[FeedEntry, ...]:
        async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
            response = await client.get(feed_url)
            response.raise_for_status()
        return parse_feed(response.text)


def parse_feed(xml_text: str) -> tuple[FeedEntry, ...]:
    """Parse RSS or Atom XML into normalized entries."""

    root = ET.fromstring(xml_text)
    if _strip_namespace(root.tag) == "rss":
        return _parse_rss(root)
    if _strip_namespace(root.tag) == "feed":
        return _parse_atom(root)
    raise ValueError("unsupported feed format")


def _parse_rss(root: ET.Element) -> tuple[FeedEntry, ...]:
    entries: list[FeedEntry] = []
    for item in root.findall("./channel/item"):
        title = _child_text(item, "title") or "Untitled"
        url = _child_text(item, "link") or _enclosure_url(item)
        if not url:
            continue
        guid = _child_text(item, "guid") or url
        entries.append(
            FeedEntry(
                id=guid,
                title=title,
                url=url,
                published_at=_parse_datetime(_child_text(item, "pubDate")),
            )
        )
    return tuple(entries)


def _parse_atom(root: ET.Element) -> tuple[FeedEntry, ...]:
    entries: list[FeedEntry] = []
    ns = _namespace(root.tag)
    prefix = f"{{{ns}}}" if ns else ""
    for entry in root.findall(f"{prefix}entry"):
        title = _child_text(entry, "title") or "Untitled"
        url = _atom_link(entry)
        if not url:
            continue
        entry_id = _child_text(entry, "id") or url
        published = _child_text(entry, "published") or _child_text(entry, "updated")
        entries.append(
            FeedEntry(
                id=entry_id,
                title=title,
                url=url,
                published_at=_parse_datetime(published),
            )
        )
    return tuple(entries)


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _strip_namespace(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _enclosure_url(item: ET.Element) -> str:
    for child in item:
        if _strip_namespace(child.tag) == "enclosure":
            return child.attrib.get("url", "").strip()
    return ""


def _atom_link(entry: ET.Element) -> str:
    alternate = ""
    for child in entry:
        if _strip_namespace(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        if not href:
            continue
        if child.attrib.get("rel", "alternate") == "alternate":
            return href
        alternate = alternate or href
    return alternate


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _namespace(tag: str) -> str:
    if tag.startswith("{") and "}" in tag:
        return tag[1:].split("}", maxsplit=1)[0]
    return ""


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]
