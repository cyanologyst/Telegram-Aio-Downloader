"""Pyrogram-native forwarded media downloader.

Uses Pyrogram's MTProto media download path:
- Pyrogram receives the forwarded message via MTProto
- download_media(message, file_name=...) gets the native file reference
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from pyrogram import filters
from pyrogram.handlers import MessageHandler

from app.services.user_settings import get_user_settings

logger = logging.getLogger(__name__)


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


def setup_pyrogram_forwarded_downloads(client, telegram_dir: str):
    """Register Pyrogram handlers for forwarded media downloads.

    Args:
        client: Pyrogram Client instance (already started).
        telegram_dir: Directory to save downloaded files.
    """
    download_dir = Path(telegram_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    async def on_forwarded_media(_, message):
        """Handle forwarded media: determine type, download, confirm."""
        user_id = message.from_user.id if message.from_user else None
        if not user_id:
            return

        settings = get_user_settings(user_id)
        if not settings.get("auto_download_forwarded_posts", False):
            logger.info("Forwarded media ignored because auto-download is disabled for user %s", user_id)
            return

        kind = "file"
        original_name = None

        if message.photo:
            kind = "photo"
            original_name = f"photo_{message.id}.jpg"
        elif message.video:
            kind = "video"
            original_name = message.video.file_name or f"video_{message.id}.mp4"
        elif message.document:
            kind = "document"
            original_name = message.document.file_name or f"document_{message.id}"
        elif message.audio:
            kind = "audio"
            original_name = message.audio.file_name or f"audio_{message.id}.mp3"
        elif message.voice:
            kind = "voice"
            original_name = f"voice_{message.id}.ogg"
        elif message.animation:
            kind = "animation"
            original_name = message.animation.file_name or f"animation_{message.id}.mp4"
        elif message.video_note:
            kind = "video_note"
            original_name = f"video_note_{message.id}.mp4"
        elif message.sticker:
            kind = "sticker"
            if message.sticker.is_animated:
                ext = "tgs"
            elif message.sticker.is_video:
                ext = "webm"
            else:
                ext = "webp"
            original_name = f"sticker_{message.id}.{ext}"
        else:
            # No downloadable media
            return

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        safe_name = re.sub(r'[\\/:\*?"<>|]+', "_", original_name)
        filename = f"{timestamp}_{safe_name}"
        target_path = download_dir / filename

        try:
            status = await message.reply("⬇️ Downloading forwarded file...")
            await client.download_media(message, file_name=str(target_path))

            if not target_path.exists():
                raise RuntimeError("Download finished but file not found on disk.")

            size = target_path.stat().st_size
            await status.edit_text(
                f"✅ Forwarded {kind} downloaded! (via Pyrogram)\n\n"
                f"📁 Folder: Telegram\n"
                f"📄 File: {target_path.name}\n"
                f"📊 Size: {_human_size(size)}"
            )
        except Exception as exc:
            logger.error("Forwarded media download failed: %s", exc)
            if target_path.exists():
                try:
                    target_path.unlink()
                except Exception:
                    pass
            await message.reply(f"❌ Failed to download forwarded media: {exc}")

    handler = MessageHandler(
        on_forwarded_media,
        filters.private
        & filters.forwarded
        & (
            filters.photo
            | filters.video
            | filters.document
            | filters.audio
            | filters.voice
            | filters.animation
            | filters.video_note
            | filters.sticker
        ),
    )
    client.add_handler(handler)
    logger.info("Pyrogram forwarded-media handler registered")
