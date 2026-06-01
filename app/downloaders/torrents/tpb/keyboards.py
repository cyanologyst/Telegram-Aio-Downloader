"""PTB inline keyboard builders for TPB crawler."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.downloaders.torrents.tpb.crawler import TPBCrawler


def tpb_categories_keyboard(query: str) -> InlineKeyboardMarkup:
    """Show TPB category buttons for a search query."""
    buttons = []
    row = []

    for key, code in TPBCrawler.CATEGORIES.items():
        label = TPBCrawler.CATEGORY_LABELS.get(key, key)
        callback = f"tpb_cat_{code}_{query[:40]}"
        row.append(InlineKeyboardButton(text=label, callback_data=callback))
        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(buttons)


def tpb_result_keyboard(torrent_id: str) -> InlineKeyboardMarkup:
    """Compact action buttons for a single TPB result."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="📥",
                    callback_data=f"tpb_dl_{torrent_id}",
                ),
                InlineKeyboardButton(
                    text="🧲",
                    callback_data=f"tpb_magnet_{torrent_id}",
                ),
                InlineKeyboardButton(
                    text="ℹ️",
                    callback_data=f"tpb_info_{torrent_id}",
                ),
            ],
        ]
    )


def tpb_header_keyboard(
    query: str,
    category: str,
    page: int,
    has_more: bool,
) -> InlineKeyboardMarkup:
    """Pagination and navigation on the header message."""
    nav_row = []

    if page > 0:
        nav_row.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"tpb_page_{category}_{query[:36]}_{page - 1}",
            )
        )

    nav_row.append(
        InlineKeyboardButton(
            text="🔍",
            callback_data="tpb_newsearch",
        )
    )

    if has_more:
        nav_row.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"tpb_page_{category}_{query[:36]}_{page + 1}",
            )
        )

    return InlineKeyboardMarkup([nav_row])


def tpb_detail_keyboard(torrent_id: str) -> InlineKeyboardMarkup:
    """Keyboard shown below a TPB detail message."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="📥 Download",
                    callback_data=f"tpb_dl_{torrent_id}",
                ),
                InlineKeyboardButton(
                    text="🧲 Magnet",
                    callback_data=f"tpb_magnet_{torrent_id}",
                ),
            ],
        ]
    )
