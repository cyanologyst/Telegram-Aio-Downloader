"""Formatting helpers for Prowlarr search results."""

from __future__ import annotations


def human_size(size_bytes: int) -> str:
    value = float(size_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size_bytes} B"


def format_prowlarr_result(item: dict, index: int) -> str:
    title = str(item.get("title") or "Unknown")[:95]
    size = human_size(int(item.get("size") or 0))
    seeders = item.get("seeders")
    leechers = item.get("leechers")
    indexer = str(item.get("indexer") or "Unknown")
    source = "magnet" if str(item.get("magnet_url") or "").startswith("magnet:") else "torrent"
    return (
        f"<b>{index}. {title}</b>\n"
        f"💾 {size}  ·  🟢 {seeders if seeders is not None else '?'}"
        f"  ·  🔴 {leechers if leechers is not None else '?'}\n"
        f"🧭 {indexer}  ·  {source}"
    )


def format_prowlarr_detail(item: dict) -> str:
    title = str(item.get("title") or "Unknown")
    size = human_size(int(item.get("size") or 0))
    categories = ", ".join(item.get("categories") or []) or "?"
    seeders = item.get("seeders")
    leechers = item.get("leechers")
    return (
        f"<b>🧭 Prowlarr · {title}</b>\n\n"
        f"Indexer: {item.get('indexer') or '?'}\n"
        f"Size: {size}\n"
        f"Seeders: {seeders if seeders is not None else '?'}\n"
        f"Leechers: {leechers if leechers is not None else '?'}\n"
        f"Categories: {categories}\n"
        f"Published: {item.get('publish_date') or '?'}"
    )
