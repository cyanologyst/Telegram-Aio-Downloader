"""Message formatting for TPB search results."""

from typing import Callable, List, Dict

from app.downloaders.torrents.tpb.crawler import TPBCrawler


def format_tpb_single(
    item: Dict,
    index: int,
    lang_func: Callable[[str], str],
) -> str:
    """Format a single TPB result into a compact HTML card."""
    title = item.get("name", "Unknown")[:90]
    size = TPBCrawler.human_size(item.get("size", "0"))
    seeders = item.get("seeders", "?")
    leechers = item.get("leechers", "?")

    return (
        f"<b>{index}. {title}</b>\n"
        f"💾 {size}  ·  🟢 {seeders}  ·  🔴 {leechers}"
    )


def format_tpb_detail(item: Dict, lang_func: Callable[[str], str]) -> str:
    """Format a single TPB torrent detail message."""
    title = item.get("name", "Unknown")
    size = TPBCrawler.human_size(item.get("size", "0"))
    seeders = item.get("seeders", "?")
    leechers = item.get("leechers", "?")
    info_hash = item.get("info_hash", "")
    added = item.get("added", "?")
    username = item.get("username", "?")

    magnet = ""
    if info_hash:
        magnet = TPBCrawler.build_magnet(info_hash, title)

    text = (
        f"<b>🏴‍☠️ {title}</b>\n\n"
        f"💾 {lang_func('size')}: {size}\n"
        f"🟢 {lang_func('seeders')}: {seeders}\n"
        f"🔴 {lang_func('leechers')}: {leechers}\n"
        f"👤 Uploader: {username}\n"
        f"📅 Added: {added}\n\n"
    )

    if magnet:
        text += f"<b>🧲 Magnet:</b> <code>{magnet}</code>"
    else:
        text += lang_func("no_magnet_stored")

    return text
