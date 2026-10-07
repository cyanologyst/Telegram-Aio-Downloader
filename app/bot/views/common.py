"""Small helpers shared by the view modules.

Views are pure functions: they take plain data and return ``(text, markup)``
with HTML text, so they can be tested without Telegram. Send them with
``parse_mode=ParseMode.HTML``.
"""

from __future__ import annotations

import html
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

Screen = tuple[str, InlineKeyboardMarkup | None]

HOME = "nav:home"


def e(value: Any) -> str:
    """Escape for Telegram HTML."""
    return html.escape(str(value), quote=False)


def short(text: Any, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def human_size(size: float) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def progress_bar(fraction: float, width: int = 12) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "▰" * filled + "▱" * (width - filled)


def on_off(value: Any) -> str:
    return "On" if value else "Off"


def button(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def rows_of(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]


def home_button(label: str = "🏠 Menu") -> InlineKeyboardButton:
    return button(label, HOME)


def back_row(
    back_data: str | None = None, back_label: str = "⬅ Back"
) -> list[InlineKeyboardButton]:
    """A Back button (when there is somewhere to go back to) next to Menu."""
    row = [button(back_label, back_data)] if back_data else []
    row.append(home_button())
    return row
