"""Message formatting for RARBG-style search results."""

from collections.abc import Callable


def format_rarbg_single(
    item: dict,
    index: int,
    lang_func: Callable[[str], str],
) -> str:
    """Format a single RARBG result into a compact HTML card."""
    title = item.get("name", "Unknown")[:90]
    size = item.get("size", "?")
    seeders = item.get("seeders", "?")
    leechers = item.get("leechers", "?")
    category = item.get("category", "?")

    return (
        f"<b>{index}. {title}</b>\n"
        f"💾 {size}  ·  🟢 {seeders}  ·  🔴 {leechers}\n"
        f"🏷 {category}"
    )


def format_rarbg_detail(item: dict, lang_func: Callable[[str], str]) -> str:
    """Format a single RARBG detail message."""
    title = item.get("name", "Unknown")
    size = item.get("size", "?")
    seeders = item.get("seeders", "?")
    leechers = item.get("leechers", "?")
    added = item.get("added", "?")
    category = item.get("category", "?")
    magnet = item.get("magnet", "")

    text = (
        f"<b>🧲 RARBG · {title}</b>\n\n"
        f"💾 {lang_func('size')}: {size}\n"
        f"🟢 {lang_func('seeders')}: {seeders}\n"
        f"🔴 {lang_func('leechers')}: {leechers}\n"
        f"🏷 Category: {category}\n"
        f"📅 Added: {added}\n\n"
    )

    if magnet:
        text += f"<b>🧲 Magnet:</b> <code>{magnet}</code>"
    else:
        text += lang_func("no_magnet_stored")

    return text
