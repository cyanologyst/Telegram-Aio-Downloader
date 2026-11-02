"""The live status dashboard message."""

from __future__ import annotations

import asyncio
import hashlib
import time

from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application

from app.bot import flood_guard
from app.bot.views import jobs as job_views
from app.bot import telegram_bot as tb


class _DashboardMessageGone(Exception):
    """Raised when a tracked status/dashboard message must be recreated."""


async def _edit_dashboard_message(
    app: Application,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup,
    parse_mode: str | None = None,
) -> bool:
    """Edit a dashboard/status message, classifying failures correctly.

    Returns True when the message now shows `text` (edited or already
    identical). Raises _DashboardMessageGone only when the target message no
    longer exists or can never be edited, so callers may safely recreate it.

    This is the fix for progress-message spam: transient errors (rate limits,
    network hiccups) must NOT cause callers to send a brand-new message.

    A rate limit never sleeps here: callers include Pyrogram progress
    callbacks, and sleeping there stalls the transfer itself (one 512 KB part
    per back-off). Instead edits to that chat are skipped until it lifts.
    """
    if not message_id or flood_guard.blocked_for(chat_id):
        return False
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return True
    except BadRequest as exc:
        lowered = str(exc).lower()
        if "message is not modified" in lowered:
            return True
        if any(
            term in lowered
            for term in (
                "message to edit not found",
                "message can't be edited",
                "there is no text in the message to edit",
                "chat not found",
                "message id is invalid",
            )
        ):
            raise tb._DashboardMessageGone() from exc
        tb.logger.warning("Status edit rejected (%s); skipping this update", exc)
        return False
    except RetryAfter as exc:
        retry_after = getattr(exc, "retry_after", 5) or 5
        if hasattr(retry_after, "total_seconds"):
            retry_after = retry_after.total_seconds()
        if not flood_guard.blocked_for(chat_id):
            tb.logger.warning(
                "Telegram rate limit on status edits in chat %s; skipping edits for %.0fs",
                chat_id, float(retry_after),
            )
        flood_guard.note(chat_id, retry_after)
        return False
    except (TimedOut, NetworkError) as exc:
        tb.logger.warning("Network error editing status message: %s", exc)
        return False


def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def build_status_screen():
    return job_views.status_screen(list(tb.download_jobs.values()), tb.free_disk_bytes())


async def update_status_message(app: Application, chat_id: int, user_id: int = None):
    """Refresh the chat's status dashboard, if one is showing. Never sends a new one."""
    lock = tb.status_message_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        msg_data = tb.status_messages.get(chat_id)
        # Re-check inside the lock: concurrent monitor loops pass a pre-lock check together.
        if not msg_data or time.time() - msg_data.get("last_update", 0) < 2.0:
            return
        text, markup = tb.build_status_screen()
        text_hash = tb._hash_content(text)
        if msg_data.get("text_hash") == text_hash:
            # Nothing visible changed; skipping the API call kills most edit traffic.
            msg_data["last_update"] = time.time()
            return
        try:
            if await tb._edit_dashboard_message(
                app, chat_id, msg_data["message_id"], text, markup, parse_mode=ParseMode.HTML
            ):
                msg_data.update(last_update=time.time(), text_hash=text_hash)
            # On a transient failure keep the same message; the next tick retries.
            # Recreating here is what used to flood the chat with duplicates.
        except tb._DashboardMessageGone:
            tb.status_messages.pop(chat_id, None)


async def show_status_dashboard(app: Application, chat_id: int, user_id: int = None, replace=None):
    """Show the status dashboard where the user is looking.

    From the keyboard or /status it is sent fresh at the bottom of the chat and
    the previous dashboard is deleted, so pressing Status always shows
    something. From an inline menu (``replace``) that message becomes the
    dashboard. Either way it is the one message auto-updates edit from then on.
    """
    text, markup = tb.build_status_screen()
    lock = tb.status_message_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        previous = tb.status_messages.pop(chat_id, None)
        message_id = None
        if replace is not None:
            try:
                await replace.edit_text(
                    text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )
                message_id = replace.message_id
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    message_id = replace.message_id
        if message_id is None:
            msg = await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            message_id = msg.message_id
        if previous and previous.get("message_id") != message_id:
            try:
                await app.bot.delete_message(chat_id=chat_id, message_id=previous["message_id"])
            except Exception as exc:
                tb.logger.debug("Old status message not deleted: %s", exc)
        tb.status_messages[chat_id] = {
            "message_id": message_id,
            "last_update": time.time(),
            "user_id": user_id or 0,
            "text_hash": tb._hash_content(text),
        }


async def maybe_auto_update_status_message(app: Application, job: dict, force: bool = False):
    """Progress hook used by every downloader: refresh the job's card and the dashboard."""
    if not job.get("status_visible", True):
        return
    # A finished job gets a fresh final card from finish_job_card(); editing
    # the progress card first would be a wasted API call.
    if job_views.is_active(job):
        await tb.refresh_job_card(app, job, force=force)

    msg_data = tb.status_messages.get(job["chat_id"])
    if not msg_data:
        return
    # One dashboard per chat, but it shares the chat's edit budget with the
    # job cards: stretch its interval as more messages are being updated.
    interval = max(tb.STATUS_AUTO_UPDATE_SECONDS, tb.edit_throttle.interval())
    if not force and time.time() - msg_data.get("last_update", 0) < interval:
        return
    await tb.update_status_message(app, job["chat_id"], job.get("user_id") or msg_data.get("user_id") or 0)
