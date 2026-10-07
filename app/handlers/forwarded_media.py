"""Pyrogram-native forwarded media downloader.

The Pyrogram client is logged in as the owner's personal account, so it sees
every private chat that account has. Only media the owner forwards *into the
bot's chat* is downloaded; forwards to friends or anywhere else are ignored.

Status messages are sent by the bot (through ``notify``), never by the
personal account: a reply from the account would land in the bot chat as a
new text message and the bot would answer it.

Uses Pyrogram's MTProto media download path, so there is no Bot API size cap:
- Pyrogram receives the forwarded message via MTProto
- download_media(message, file_name=...) gets the native file reference
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pyrogram import filters
from pyrogram.handlers import MessageHandler

from app.services.user_settings import get_user_settings

logger = logging.getLogger(__name__)

# notify(user_id, text, message_id) sends a new status message when
# message_id is None, otherwise edits that message. Returns the message id.
Notifier = Callable[[int, str, int | None], Awaitable[int | None]]

MEDIA_FILTER = (
    filters.photo
    | filters.video
    | filters.document
    | filters.audio
    | filters.voice
    | filters.animation
    | filters.video_note
    | filters.sticker
)


def _human_size(size: int) -> str:
    """Format bytes to human-readable size."""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{size} B"


def bot_id_from_token(bot_token: str) -> int:
    """A bot's user ID is the numeric prefix of its token."""
    prefix = (bot_token or "").split(":", 1)[0]
    if not prefix.isdigit():
        raise ValueError("BOT_TOKEN does not start with the bot's numeric ID")
    return int(prefix)


def describe_media(message: Any) -> tuple[str, str] | None:
    """Return (kind, original file name) for a media message, or None."""
    if message.photo:
        return "photo", f"photo_{message.id}.jpg"
    if message.video:
        return "video", message.video.file_name or f"video_{message.id}.mp4"
    if message.document:
        return "document", message.document.file_name or f"document_{message.id}"
    if message.audio:
        return "audio", message.audio.file_name or f"audio_{message.id}.mp3"
    if message.voice:
        return "voice", f"voice_{message.id}.ogg"
    if message.animation:
        return "animation", message.animation.file_name or f"animation_{message.id}.mp4"
    if message.video_note:
        return "video_note", f"video_note_{message.id}.mp4"
    if message.sticker:
        if message.sticker.is_animated:
            ext = "tgs"
        elif message.sticker.is_video:
            ext = "webm"
        else:
            ext = "webp"
        return "sticker", f"sticker_{message.id}.{ext}"
    return None


def make_forwarded_media_callback(
    client: Any,
    download_dir: Path,
    notify: Notifier,
) -> Callable[[Any, Any], Awaitable[None]]:
    """Build the Pyrogram callback; split out so it can be tested directly."""

    async def on_forwarded_media(_: Any, message: Any) -> None:
        user_id = message.from_user.id if message.from_user else None
        if not user_id:
            return

        media = describe_media(message)
        if media is None:
            return
        kind, original_name = media

        settings = get_user_settings(user_id)
        if not settings.get("auto_download_forwarded_posts", False):
            await notify(
                user_id,
                "Forwarded media is not downloaded because auto-download is off.\n"
                "Turn it on in Settings → Forwarded posts, then forward it again.",
                None,
            )
            return

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = re.sub(r'[\\/:\*?"<>|]+', "_", original_name)
        target_path = download_dir / f"{timestamp}_{safe_name}"

        status_id = await notify(user_id, f"⬇️ Downloading forwarded {kind}...", None)
        try:
            await client.download_media(message, file_name=str(target_path))

            if not target_path.exists():
                raise RuntimeError("Download finished but file not found on disk.")

            size = target_path.stat().st_size
            await notify(
                user_id,
                f"✅ Forwarded {kind} downloaded\n\n"
                f"📁 Folder: Telegram\n"
                f"📄 File: {target_path.name}\n"
                f"📊 Size: {_human_size(size)}",
                status_id,
            )
        except Exception as exc:
            logger.error("Forwarded media download failed: %s", exc)
            if target_path.exists():
                with suppress(Exception):
                    target_path.unlink()
            await notify(user_id, f"❌ Failed to download forwarded {kind}: {exc}", status_id)

    return on_forwarded_media


def setup_pyrogram_forwarded_downloads(
    client: Any,
    telegram_dir: str,
    *,
    bot_id: int,
    notify: Notifier,
) -> None:
    """Register the Pyrogram handler for media forwarded into the bot chat.

    Args:
        client: Pyrogram Client instance (already started), logged in as the owner.
        telegram_dir: Directory to save downloaded files.
        bot_id: The bot's user ID; only the owner's chat with the bot is watched.
        notify: Sends or edits status messages as the bot.
    """
    download_dir = Path(telegram_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    handler = MessageHandler(
        make_forwarded_media_callback(client, download_dir, notify),
        filters.chat(bot_id) & filters.outgoing & filters.forwarded & MEDIA_FILTER,
    )
    client.add_handler(handler)
    logger.info("Pyrogram forwarded-media handler registered for bot chat %s", bot_id)
