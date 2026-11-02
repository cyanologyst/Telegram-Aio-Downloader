"""Pyrogram client and raw sends with FloodWait handling."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys

from pyrogram.errors import FloodWait, RPCError

from app.bot import telegram_bot as tb


async def send_with_flood_wait_handling(send_coroutine, max_retries: int = 5):
    """
    Wraps a pyrogram send operation with FloodWait handling.
    Automatically waits and retries when Telegram rate-limits.

    Args:
        send_coroutine: The async send operation (e.g., client.send_document)
        max_retries: Maximum number of retries before giving up

    Returns:
        The result of the send operation

    Raises:
        FloodWait: If max_retries is exceeded
    """
    for attempt in range(max_retries):
        try:
            return await send_coroutine()
        except FloodWait as e:
            wait_time = e.value
            if wait_time > tb.MAX_FLOOD_WAIT:
                # Callers hold the chat's send chain (and the preview lock):
                # sleeping hours here would stall every other upload too.
                tb.logger.error(
                    "FloodWait of %ss is too long to wait out; giving up on this send", wait_time
                )
                raise
            tb.logger.warning(
                f"FloodWait: Telegram rate-limiting. "
                f"Waiting {wait_time} seconds (attempt {attempt + 1}/{max_retries})"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(wait_time)
            else:
                tb.logger.error(f"FloodWait: Max retries exceeded after {max_retries} attempts")
                raise
        except Exception as e:
            tb.logger.error(f"Error in send operation: {e}")
            raise


class PyrogramUnavailable(RuntimeError):
    """No Pyrogram user login is available; the bot runs in bot-only mode."""


def pyrogram_unavailable_reason() -> str | None:
    """Why the Pyrogram user client can't be used, or None when it can."""
    if not tb.API_ID or not tb.API_HASH:
        return "API_ID/API_HASH are not set"
    if tb.PYRO_SESSION_STRING or (tb.BASE_DIR / f"{tb.PYRO_SESSION_NAME}.session").exists():
        return None
    if sys.stdin is not None and sys.stdin.isatty():
        return None  # first run in a terminal: Pyrogram asks for phone and code
    return (
        "there is no Pyrogram login yet (run `python main.py` once in a terminal to "
        "log in, or set PYRO_SESSION_STRING)"
    )


async def get_pyrogram_client():
    """The started user-account client, starting it on first use.

    Callers race here (uploads, previews, notices); the lock makes them share
    one client instead of opening the same session file twice. The client is
    only kept once it started, so a failed start is retried by the next call
    rather than handing out a client that was never connected.
    """

    if tb.pyro_client is not None:
        return tb.pyro_client
    lock = tb._pyro_start_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
    async with lock:
        if tb.pyro_client is not None:
            return tb.pyro_client
        reason = tb.pyrogram_unavailable_reason()
        if reason:
            raise tb.PyrogramUnavailable(reason)
        session_kwargs = (
            {"session_string": tb.PYRO_SESSION_STRING, "in_memory": True}
            if tb.PYRO_SESSION_STRING
            else {}
        )
        client = tb.Client(
            tb.PYRO_SESSION_NAME,
            api_id=tb.API_ID,
            api_hash=tb.API_HASH,
            workdir=str(tb.BASE_DIR),
            **session_kwargs,
        )
        await client.start()
        try:
            me = await client.get_me()
        except Exception:
            await client.stop()
            raise
        if getattr(me, "is_bot", False):
            await client.stop()
            raise RuntimeError(
                f"Pyrogram session is logged in as a bot.\n"
                f'Delete "{tb.PYRO_SESSION_NAME}.session" and log in with your personal Telegram account.'
            )
        tb.pyro_client = client

    return tb.pyro_client


async def stop_pyrogram_client():
    if tb.pyro_client is not None:
        with contextlib.suppress(Exception):
            await tb.pyro_client.stop()
        tb.pyro_client = None


async def pyrogram_send_file(
    rel_path: str,
    progress_callback=None,
    chat_id: int | str = "me",
    *,
    force_document: bool = False,
    max_size: int | None = None,
):
    """Send one file with the user account, to Saved Messages unless ``chat_id`` is given.

    ``force_document`` sends it as a plain file (byte-split parts can't play).
    """
    client = await tb.get_pyrogram_client()
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)

    if not full.exists():
        raise FileNotFoundError(str(full))
    if full.is_dir():
        raise IsADirectoryError(str(full))

    size = full.stat().st_size
    if size > (max_size or tb.MAX_SEND_SIZE):
        raise ValueError(f"File exceeds configured limit: {tb.human_size(size)}")

    caption = tb.build_upload_caption(rel_path)
    send_method = "document" if force_document else tb.get_send_method(str(full))
    thumbnail_path = None

    try:
        # Generate thumbnail only when valid
        if send_method in ("video", "photo"):
            tb.cleanup_orphan_thumbnails(full.parent)
            thumb = tb.generate_thumbnail(str(full))

            # FIX: avoid passing empty string to Pyrogram
            thumbnail_path = thumb if thumb and os.path.exists(thumb) else None  # noqa: ASYNC240

        if send_method == "video":
            video_meta = tb.get_video_metadata(str(full))

            await tb.send_with_flood_wait_handling(
                lambda: client.send_video(
                    chat_id=chat_id,
                    video=str(full),
                    caption=caption,
                    thumb=thumbnail_path,
                    supports_streaming=True,
                    **tb.video_send_kwargs(video_meta),
                    progress=progress_callback,
                )
            )

        elif send_method == "audio":
            await tb.send_with_flood_wait_handling(
                lambda: client.send_audio(
                    chat_id=chat_id,
                    audio=str(full),
                    caption=caption,
                    progress=progress_callback,
                )
            )

        elif send_method == "photo":
            await tb.send_with_flood_wait_handling(
                lambda: client.send_photo(
                    chat_id=chat_id,
                    photo=str(full),
                    caption=caption,
                    progress=progress_callback,
                )
            )

        else:
            await tb.send_with_flood_wait_handling(
                lambda: client.send_document(
                    chat_id=chat_id,
                    document=str(full),
                    caption=caption,
                    thumb=thumbnail_path,
                    progress=progress_callback,
                )
            )

    finally:
        # Clean up thumbnail if created
        if thumbnail_path and os.path.exists(thumbnail_path):  # noqa: ASYNC240
            with contextlib.suppress(Exception):
                os.remove(thumbnail_path)


def _raise_if_bot_session(exc: RPCError) -> None:
    msg = str(exc)
    if "USER_IS_BOT" in msg or "A bot cannot send messages to other bots or to itself" in msg:
        raise RuntimeError(tb.PYROGRAM_BOT_SESSION_HINT) from exc
