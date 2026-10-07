"""Settings hub and the archive settings sub-screen."""

from __future__ import annotations

import time
from typing import Any

from telegram import InlineKeyboardMarkup

from app.bot.views.common import Screen, back_row, button, e, on_off
from app.services.batch_download import batch_download_mode_label
from app.services.cookies import CookieSummary

# What a video link does: ask with a quality picker, or start right away.
VIDEO_DEFAULTS = ["ask", "best", "1080", "720", "480", "mp3"]
VIDEO_DEFAULT_LABELS = {
    "ask": "Ask each time",
    "best": "Best quality",
    "1080": "Up to 1080p",
    "720": "Up to 720p",
    "480": "Up to 480p",
    "mp3": "MP3 audio",
}

# Toggles and cycles reachable from the hub, as set:<key> callbacks.
HUB_TOGGLES = {
    "auto_upload_after_download",
    "send_gifs_to_chat",
    "auto_delete_files_after_upload",
    "auto_download_forwarded_posts",
    "manga_auto_convert_pdf",
    "manga_remove_images_after_conversion",
}
HUB_CYCLES = {"video_default", "batch_download_mode"}


def video_default_label(value: Any) -> str:
    return VIDEO_DEFAULT_LABELS.get(str(value), VIDEO_DEFAULT_LABELS["ask"])


def next_video_default(value: Any) -> str:
    current = str(value) if str(value) in VIDEO_DEFAULTS else "ask"
    return VIDEO_DEFAULTS[(VIDEO_DEFAULTS.index(current) + 1) % len(VIDEO_DEFAULTS)]


def archive_summary(s: dict[str, Any]) -> str:
    part_mb = int(s.get("zip_part_size", 0)) // (1024 * 1024)
    part = f"{part_mb // 1024} GB" if part_mb and part_mb % 1024 == 0 else f"{part_mb} MB"
    password = "password set" if s.get("password") else "no password"
    return (
        f"{str(s.get('zip_method', 'zip')).upper()} · level {s.get('compression_level', 3)} · "
        f"{part} parts · {password}"
    )


def cookies_label(summary: CookieSummary | None) -> str:
    if summary is None:
        return "none"
    if "youtube.com" in summary.sites:
        return "YouTube ✓"
    return f"{summary.count} saved"


def settings_screen(s: dict[str, Any], cookies: CookieSummary | None = None) -> Screen:
    text = "\n".join(
        [
            "⚙️ <b>Settings</b>",
            "",
            "<b>Downloads</b>",
            f"• Video links: {e(video_default_label(s.get('video_default')))}",
            f"• Send GIFs to this chat: {on_off(s.get('send_gifs_to_chat', True))}",
            f"• Cookies: {e(cookies_label(cookies))}",
            f"• Upload to Saved Messages when done: {on_off(s.get('auto_upload_after_download'))}",
            f"• Delete files after uploading: {on_off(s.get('auto_delete_files_after_upload'))}",
            f"• Playlists and model pages: {e(batch_download_mode_label(s.get('batch_download_mode')))}",
            f"• Download forwarded media: {on_off(s.get('auto_download_forwarded_posts'))}",
            "",
            "<b>Archives</b>",
            f"• {e(archive_summary(s))}",
            "",
            "<b>Manga</b>",
            f"• Make a PDF after download: {on_off(s.get('manga_auto_convert_pdf'))}",
            f"• Remove images after PDF: {on_off(s.get('manga_remove_images_after_conversion'))}",
            "",
            "Tap a button to change it.",
        ]
    )
    rows = [
        [
            button(
                f"🎬 Video links: {video_default_label(s.get('video_default'))}",
                "set:video_default",
            )
        ],
        [
            button(
                f"🎞 GIFs to chat: {on_off(s.get('send_gifs_to_chat', True))}",
                "set:send_gifs_to_chat",
            ),
            button(f"🍪 Cookies: {cookies_label(cookies)}", "nav:cookies"),
        ],
        [
            button(
                f"📤 Upload when done: {on_off(s.get('auto_upload_after_download'))}",
                "set:auto_upload_after_download",
            ),
        ],
        [
            button(
                f"🗑 Delete after upload: {on_off(s.get('auto_delete_files_after_upload'))}",
                "set:auto_delete_files_after_upload",
            ),
        ],
        [
            button(
                f"🎞 Playlists: {batch_download_mode_label(s.get('batch_download_mode'))}",
                "set:batch_download_mode",
            )
        ],
        [
            button(
                f"📨 Forwarded media: {on_off(s.get('auto_download_forwarded_posts'))}",
                "set:auto_download_forwarded_posts",
            )
        ],
        [button("📦 Archive settings ›", "nav:archive_settings")],
        [
            button(
                f"🖼 Manga PDF: {on_off(s.get('manga_auto_convert_pdf'))}",
                "set:manga_auto_convert_pdf",
            ),
            button(
                f"Remove images: {on_off(s.get('manga_remove_images_after_conversion'))}",
                "set:manga_remove_images_after_conversion",
            ),
        ],
        back_row(),
    ]
    return text, InlineKeyboardMarkup(rows)


def archive_settings_screen(s: dict[str, Any], back: str = "nav:settings") -> Screen:
    part_mb = int(s.get("zip_part_size", 0)) // (1024 * 1024)
    lines = [
        "📦 <b>Archive settings</b>",
        "",
        f"Format: {e(str(s.get('zip_method', 'zip')).upper())}",
        f"Compression level: {s.get('compression_level', 3)}/9",
        f"Split into parts of: {part_mb} MB",
        f"Password: {'set' if s.get('password') else 'none'}",
        f"Delete files after zipping: {on_off(s.get('auto_delete_files_after_zip'))}",
        f"Delete archives after sending: {on_off(s.get('auto_delete_zips_after_send'))}",
    ]
    if str(s.get("zip_method", "zip")).lower() == "7z" and int(s.get("compression_level", 3)) >= 7:
        lines += ["", "⚠️ 7Z at level 7+ is slow and CPU-heavy."]
    rows = [
        [
            button(f"📋 Format: {str(s.get('zip_method', 'zip')).upper()}", "zip_setting:method"),
            button(f"🔨 Level: {s.get('compression_level', 3)}", "zip_setting:compression"),
        ],
        [
            button(f"✂️ Parts: {part_mb} MB", "zip_setting:part_size"),
            button(
                f"🔐 Password: {'Set' if s.get('password') else 'None'}", "zip_setting:password"
            ),
        ],
        [
            button(
                f"🗑 Delete files after zip: {on_off(s.get('auto_delete_files_after_zip'))}",
                "zip_setting:auto_del_files",
            )
        ],
        [
            button(
                f"🗑 Delete archives after send: {on_off(s.get('auto_delete_zips_after_send'))}",
                "zip_setting:auto_del_zips",
            )
        ],
        back_row(back),
    ]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def archive_choice_screen(title: str, options: list[tuple[str, str]], back: str) -> Screen:
    """A list of values for one archive setting (format, level, part size)."""
    rows = [[button(label, data)] for label, data in options]
    rows.append(back_row(back))
    return f"📦 <b>{e(title)}</b>", InlineKeyboardMarkup(rows)


def _age(saved_at: float | None) -> str:
    if not saved_at:
        return ""
    days = int((time.time() - saved_at) // 86400)
    return "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"


def cookies_screen(
    summary: CookieSummary | None,
    *,
    waiting: bool = False,
    from_env: bool = False,
    confirm_delete: bool = False,
) -> Screen:
    lines = ["🍪 <b>Cookies</b>", ""]
    if summary is None:
        lines.append("No cookies yet.")
    else:
        sites = ", ".join(summary.sites[:4]) + (" …" if len(summary.sites) > 4 else "")
        source = "from YTDLP_COOKIES_FILE" if from_env else f"added {_age(summary.saved_at)}"
        lines.append(f"{summary.count} cookies for {e(sites)} · {e(source)}")
        if summary.expired:
            lines.append(f"⚠️ {summary.expired} of them have expired; export a fresh file.")
    lines += [
        "",
        "YouTube blocks many servers with “confirm you're not a bot”, and Spotify "
        "downloads get their audio from YouTube. Cookies from a logged-in browser fix "
        "that, and unlock private or age-restricted videos on other sites too.",
        "",
        "<b>How to get them</b>",
        "1. Use a spare Google account, not your main one.",
        "2. Open a private/incognito window and log in to youtube.com.",
        "3. Export cookies with the “Get cookies.txt LOCALLY” extension (Netscape format).",
        "4. Close the private window, so YouTube doesn't replace those cookies.",
        "5. Send the file here.",
        "",
        "The bot deletes your message with the file and keeps it on the server, "
        "readable only by the bot.",
    ]
    if waiting:
        lines += ["", "📎 <b>Send the cookies.txt file now</b> (as a file). /cancel to stop."]
        rows = [[button("✖ Cancel", "ck:cancel")]]
        return "\n".join(lines), InlineKeyboardMarkup(rows)
    if confirm_delete:
        lines += ["", "Remove the saved cookies?"]
        rows = [[button("🗑 Yes, remove", "ck:dely"), button("Keep them", "nav:cookies")]]
        return "\n".join(lines), InlineKeyboardMarkup(rows)
    first = [button("📎 Send cookies.txt" if summary is None else "📎 Replace", "ck:send")]
    if summary is not None and not from_env:
        first.append(button("🗑 Remove", "ck:del"))
    return "\n".join(lines), InlineKeyboardMarkup([first, back_row("nav:settings")])
