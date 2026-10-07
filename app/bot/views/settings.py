"""Settings hub and the archive settings sub-screen."""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardMarkup

from app.bot.views.common import Screen, back_row, button, e, on_off
from app.services.batch_download import batch_download_mode_label

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


def _archive_summary(s: dict[str, Any]) -> str:
    part_mb = int(s.get("zip_part_size", 0)) // (1024 * 1024)
    part = f"{part_mb // 1024} GB" if part_mb and part_mb % 1024 == 0 else f"{part_mb} MB"
    password = "password set" if s.get("password") else "no password"
    return (
        f"{str(s.get('zip_method', 'zip')).upper()} · level {s.get('compression_level', 3)} · "
        f"{part} parts · {password}"
    )


def settings_screen(s: dict[str, Any]) -> Screen:
    text = "\n".join(
        [
            "⚙️ <b>Settings</b>",
            "",
            "<b>Downloads</b>",
            f"• Video links: {e(video_default_label(s.get('video_default')))}",
            f"• Upload to Saved Messages when done: {on_off(s.get('auto_upload_after_download'))}",
            f"• Delete files after uploading: {on_off(s.get('auto_delete_files_after_upload'))}",
            f"• Playlists and model pages: {e(batch_download_mode_label(s.get('batch_download_mode')))}",
            f"• Download forwarded media: {on_off(s.get('auto_download_forwarded_posts'))}",
            "",
            "<b>Archives</b>",
            f"• {e(_archive_summary(s))}",
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
