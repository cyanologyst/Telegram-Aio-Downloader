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
                InlineKeyboardButton("📥 All", callback_data=f"prowlarr_dl_{token}"),
                InlineKeyboardButton("☑️ Select", callback_data=f"prowlarr_select_{token}"),
                InlineKeyboardButton("ℹ️", callback_data=f"prowlarr_info_{token}"),
            ]
        ]
    )


def prowlarr_header_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔍 New Search", callback_data="prowlarr_newsearch")]]
    )
