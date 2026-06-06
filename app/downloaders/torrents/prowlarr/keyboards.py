"""Inline keyboards for Prowlarr search."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.downloaders.torrents.prowlarr.client import ProwlarrClient


def prowlarr_categories_keyboard(query: str) -> InlineKeyboardMarkup:
    rows = []
    row = []
    token = query[:36]
    for key, label in ProwlarrClient.CATEGORY_LABELS.items():
        row.append(InlineKeyboardButton(label, callback_data=f"prowlarr_cat_{key}_{token}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def prowlarr_result_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Download", callback_data=f"prowlarr_dl_{token}"),
                InlineKeyboardButton("Select Files", callback_data=f"prowlarr_select_{token}"),
                InlineKeyboardButton("Info", callback_data=f"prowlarr_info_{token}"),
            ]
        ]
    )


def prowlarr_header_keyboard(page: int = 0, total_pages: int = 1) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Prev", callback_data=f"prowlarr_page_{page - 1}"))
    nav.append(InlineKeyboardButton("New Search", callback_data="prowlarr_newsearch"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("Next", callback_data=f"prowlarr_page_{page + 1}"))
    return InlineKeyboardMarkup([nav])
