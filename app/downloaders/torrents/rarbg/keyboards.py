"""PTB inline keyboard builders for RARBG-style crawler."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.downloaders.torrents.rarbg.crawler import RARBGCrawler


def rarbg_categories_keyboard(query: str) -> InlineKeyboardMarkup:
    """Show RARBG category buttons for a search query."""
    buttons = []
    row = []
    encoded = RARBGCrawler.safe_query(query)

    for key, code in RARBGCrawler.CATEGORIES.items():
        label = RARBGCrawler.CATEGORY_LABELS.get(key, key)
        callback = f"rarbg_cat_{code or 'all'}_{encoded}"
        row.append(InlineKeyboardButton(text=label, callback_data=callback))
        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(buttons)


def rarbg_result_keyboard(torrent_id: str) -> InlineKeyboardMarkup:
    """Compact action buttons for a single RARBG result."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(text="📥", callback_data=f"rarbg_dl_{torrent_id}"),
                InlineKeyboardButton(text="🧲", callback_data=f"rarbg_magnet_{torrent_id}"),
                InlineKeyboardButton(text="ℹ️", callback_data=f"rarbg_info_{torrent_id}"),
            ],
        ]
    )


def rarbg_header_keyboard(
    query: str,
    category: str,
    page: int,
    has_more: bool,
) -> InlineKeyboardMarkup:
    """Pagination and navigation on the header message."""
    encoded = RARBGCrawler.safe_query(query)
    nav_row = []

    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"rarbg_page_{category or 'all'}_{encoded}_{page - 1}",
            )
        )

    nav_row.append(InlineKeyboardButton(text="🔍", callback_data="rarbg_newsearch"))

    if has_more:
        nav_row.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"rarbg_page_{category or 'all'}_{encoded}_{page + 1}",
            )
        )

    return InlineKeyboardMarkup([nav_row])


def rarbg_detail_keyboard(torrent_id: str) -> InlineKeyboardMarkup:
    """Keyboard shown below a RARBG detail message."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(text="📥 Download", callback_data=f"rarbg_dl_{torrent_id}"),
                InlineKeyboardButton(text="🧲 Magnet", callback_data=f"rarbg_magnet_{torrent_id}"),
            ],
        ]
    )
