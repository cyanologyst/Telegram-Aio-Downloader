"""Home hub, bottom keyboard and help screen."""

from __future__ import annotations

from dataclasses import dataclass

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from app.bot.views.common import Screen, button, e, home_button, human_size

# Bottom-keyboard labels. on_text matches them after stripping emoji, so the
# words must stay unique: status, files, search, settings, menu, help.
KEY_STATUS = "📊 Status"
KEY_FILES = "📁 Files"
KEY_SEARCH = "🔍 Search"
KEY_SETTINGS = "⚙️ Settings"
KEY_MENU = "🏠 Menu"
KEY_HELP = "❓ Help"

PLACEHOLDER = "Send a link, magnet or .torrent file…"

# Bot command menu shown by Telegram next to the text box.
COMMANDS = [
    ("start", "Show the keyboard and a short guide"),
    ("status", "Active downloads"),
    ("files", "Browse downloaded files"),
    ("search", "Search torrents"),
    ("settings", "Settings"),
    ("cancel", "Stop what the bot is waiting for"),
    ("clear", "Clear finished jobs from the list"),
    ("sites", "Supported sites"),
    ("help", "How to use the bot"),
]


@dataclass(frozen=True)
class MiniApp:
    url: str = ""
    enabled: bool = False

    @property
    def native(self) -> bool:
        """Telegram only opens WebApp buttons for public HTTPS URLs."""
        return self.enabled and self.url.lower().startswith("https://")

    def inline_button(self, label: str = "📱 Mini App") -> InlineKeyboardButton | None:
        if not self.enabled or not self.url:
            return None
        if self.native:
            return InlineKeyboardButton(label, web_app=WebAppInfo(url=self.url))
        return InlineKeyboardButton(f"{label} (browser)", url=self.url)


def reply_keyboard(mini_app: MiniApp) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton | str]] = [
        [KEY_STATUS, KEY_FILES],
        [KEY_SEARCH, KEY_SETTINGS],
        [KEY_MENU, KEY_HELP],
    ]
    if mini_app.native:
        rows.insert(0, [KeyboardButton("📱 Mini App", web_app=WebAppInfo(url=mini_app.url))])
    return ReplyKeyboardMarkup(
        rows, resize_keyboard=True, is_persistent=True, input_field_placeholder=PLACEHOLDER
    )


def welcome_text() -> str:
    return (
        "👋 <b>Ready.</b>\n\n"
        "Send me any of these to start a download:\n"
        "• a magnet link or a <b>.torrent</b> file\n"
        "• a direct file link (ending in .zip, .mkv, …)\n"
        "• a YouTube, social-media or other video link\n"
        "• a Spotify link\n"
        "• a manga or gallery link\n\n"
        "Use the keyboard below for status, files, search and settings."
    )


def home_screen(active_jobs: int, free_bytes: int | None, mini_app: MiniApp) -> Screen:
    lines = ["🏠 <b>Menu</b>", ""]
    lines.append(
        f"📥 Active downloads: <b>{active_jobs}</b>" if active_jobs else "📥 No active downloads"
    )
    if free_bytes is not None:
        lines.append(f"💽 Free disk space: <b>{e(human_size(free_bytes))}</b>")
    lines += ["", "Send a link or a .torrent file at any time to start a download."]
    rows = [
        [button("📊 Status", "nav:status"), button("📁 Files", "fb:list:0:")],
        [button("🔍 Search torrents", "srch:p:"), button("📦 Archive", "nav:archive")],
        [button("⚙️ Settings", "nav:settings"), button("❓ Help", "nav:help")],
    ]
    mini = mini_app.inline_button()
    if mini:
        rows.append([mini])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def help_screen(supported_sites_url: str) -> Screen:
    text = (
        "❓ <b>How to use</b>\n\n"
        "<b>Download</b> — send a magnet, a .torrent file, a direct file link, a "
        "YouTube or other video link, a Spotify link, or a manga/gallery link. "
        "Each download gets its own card with live progress and a Cancel button.\n\n"
        "<b>Search</b> — 🔍 Search finds torrents on Prowlarr, The Pirate Bay or a "
        "RARBG mirror.\n\n"
        "<b>Files</b> — 📁 Files browses the download folder. Tap a file to upload "
        "it to your Saved Messages, convert or delete it; use Select for several "
        "files at once.\n\n"
        "<b>Archive</b> — pack files into ZIP or 7Z, optionally password-protected "
        "and split into parts.\n\n"
        "<b>Forwarded media</b> — forward a post here to save its media (turn it "
        "on in Settings).\n\n"
        "<b>Commands</b>\n" + " · ".join(f"/{name}" for name, _ in COMMANDS)
    )
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🌐 Supported sites", url=supported_sites_url)],
            [home_button()],
        ]
    )
    return text, markup


def sites_screen(supported_sites_url: str) -> Screen:
    text = (
        "🌐 <b>Supported sites</b>\n\n"
        "The full list of video, music, manga and torrent sources is kept up to "
        "date on the project page."
    )
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open the list", url=supported_sites_url)], [home_button()]]
    )
    return text, markup
