"""Per-job cards: rendering, attaching, refreshing and finishing."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import Application

from app.bot import flood_guard
from app.bot import telegram_bot as tb
from app.bot.views import jobs as job_views
from app.services import upload_filter


def _abs_download_path(path) -> Path | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = tb.DOWNLOAD_DIR / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(tb.DOWNLOAD_DIR.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.exists() else None


def job_outputs(job: dict) -> list[str]:
    """Files/folders a finished job produced, relative to Download/, that still exist."""
    candidates = list(job.get("outputs") or [])
    if job.get("filepath"):
        candidates.append(job["filepath"])
    if job.get("pdf_path"):
        candidates.append(job["pdf_path"])
    elif job.get("provider") == "manga" and job.get("folder") and job.get("status") == "completed":
        candidates.append(job["folder"])
    candidates += list(job.get("files") or [])  # aria2: the selected files

    rels: list[str] = []
    root = tb.DOWNLOAD_DIR.resolve()
    for candidate in candidates:
        resolved = tb._abs_download_path(candidate)
        if resolved is None or resolved == root:
            continue
        rel = str(resolved.relative_to(root)).replace("\\", "/")
        if rel not in rels:
            rels.append(rel)
    return rels


def job_open_data(outputs: list[str]) -> str | None:
    """Callback data for the card's Open button: the file, or the folder holding the outputs."""
    if not outputs:
        return None
    if len(outputs) == 1:
        full = tb.DOWNLOAD_DIR / outputs[0]
        if full.is_file():
            return f"fb:file:0:{tb.encode_path(outputs[0])}"
        return f"fb:list:0:{tb.encode_path(outputs[0])}"
    common = os.path.commonpath(outputs) if len(outputs) > 1 else outputs[0]
    common = "" if common in (".", "/") else common
    return f"fb:list:0:{tb.encode_path(common)}"


def render_job_card(job: dict, confirm: str | None = None):
    outputs = tb.job_outputs(job) if job.get("status") == "completed" else []
    has_video = bool(outputs) and tb.job_preview_video(job) is not None
    return job_views.job_card(
        job,
        open_data=tb.job_open_data(outputs),
        can_upload=bool(outputs),
        can_preview=has_video,
        can_compress=has_video,
        confirm=confirm,
    )


async def _send_card(app: Application, job: dict):
    text, markup = tb.render_job_card(job)
    msg = await app.bot.send_message(
        chat_id=job["chat_id"],
        text=text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    job["card"] = {
        "chat_id": job["chat_id"],
        "message_id": msg.message_id,
        "hash": tb._hash_content(text + repr(markup)),
        "last": time.time(),
    }
    tb.edit_throttle.mark(
        job["card"], card_throttle_key(job), card_progress(job), phase=job.get("status")
    )


async def attach_job_card(app: Application, job: dict, message=None):
    """Give a job its card: turn ``message`` (e.g. the link prompt) into it, or send one."""
    if job.get("card_final"):
        # Finished before we got here; its final card is already in the chat.
        if message is not None:
            with contextlib.suppress(Exception):
                await message.delete()
        return
    if message is not None:
        text, markup = tb.render_job_card(job)
        try:
            await message.edit_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
            job["card"] = {
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "hash": tb._hash_content(text + repr(markup)),
                "last": time.time(),
            }
            tb.edit_throttle.mark(
                job["card"], card_throttle_key(job), card_progress(job), phase=job.get("status")
            )
            return
        except BadRequest as exc:
            tb.logger.debug("Could not turn the prompt into a job card: %s", exc)
    try:
        await tb._send_card(app, job)
    except Exception as exc:
        tb.logger.warning("Could not send job card for #%s: %s", job.get("id"), exc)


def card_throttle_key(job: dict) -> tuple:
    return ("card", job.get("id"))


def card_progress(job: dict) -> float | None:
    """Percent done for the throttle, or None while the size is unknown."""
    total = int(job.get("total_length") or 0)
    if total:
        return int(job.get("completed_length") or 0) * 100.0 / total
    if job.get("provider") in job_views.BATCH_PROVIDERS:
        count = int(job.get("episode_count") or job.get("video_count") or 0)
        if count:
            return int(job.get("completed_items") or 0) * 100.0 / count
    return None


async def refresh_job_card(
    app: Application, job: dict, force: bool = False, confirm: str | None = None
):
    card = job.get("card")
    if not card or job.get("card_final"):
        return
    # Milestones (5 %), status changes or the heartbeat; the gap widens with
    # every other live message so the chat stays clear of FloodWait.
    if not tb.edit_throttle.due(
        card, card_throttle_key(job), card_progress(job), phase=job.get("status"), force=force
    ):
        return
    text, markup = tb.render_job_card(job, confirm=confirm)
    content_hash = tb._hash_content(text + repr(markup))
    if content_hash == card.get("hash"):
        card["last"] = time.time()
        return
    try:
        if await tb._edit_dashboard_message(
            app, card["chat_id"], card["message_id"], text, markup, parse_mode=ParseMode.HTML
        ):
            card.update(hash=content_hash, last=time.time())
    except tb._DashboardMessageGone:
        job.pop("card", None)
        await tb._send_card(app, job)


async def finish_job_card(app: Application, job: dict):
    """Show the job's final state as a fresh card at the bottom of the chat.

    A new message (instead of an edit) means Telegram notifies the user that
    the download finished; the progress card it replaces is deleted. GIFs the
    job produced are sent first, so the card stays the last message.
    """
    if job.get("card_final") == job.get("status") or not job.get("status_visible", True):
        return
    tb.edit_throttle.forget(card_throttle_key(job))
    await tb.send_job_gifs(app, job)
    old = job.get("card")
    settings = tb.get_user_settings(job.get("user_id") or 0)
    # Once per job, even when this runs again for a card held back by flood control.
    auto_upload = (
        job.get("status") == "completed"
        and settings.get("auto_upload_after_download")
        and not job.get("auto_upload_started")
        and bool(tb.job_outputs(job))
    )
    upload_list: list[str] = []
    if auto_upload:
        preset = settings.get("auto_upload_types")
        upload_list, deleted = await asyncio.to_thread(tb.filter_auto_upload_files, job, preset)
        if upload_list:
            job["auto_upload_note"] = f"📤 Uploading to {tb.upload_target_label(settings)}…"
            if deleted:
                job[
                    "auto_upload_note"
                ] += f" ({upload_filter.label(preset)}; {deleted} other file(s) deleted)"
        else:
            auto_upload = False
            job["auto_upload_note"] = upload_filter.filtered_out_note(preset, deleted)
    try:
        await tb._send_card(app, job)
        job["card_final"] = job.get("status")
    except RetryAfter as exc:
        # Flood control: the upload goes through the user account, so it must not
        # wait 14 hours for the card. The card is posted when the block lifts.
        flood_guard.note(job["chat_id"], tb._retry_seconds(exc))
        if not any(
            item.get("job_id") == job.get("id") for item in flood_guard.held(job["chat_id"])
        ):
            tb.logger.info("Final card for #%s held until flood control lifts", job.get("id"))
            tb.hold_message(app, job["chat_id"], {"job_id": job.get("id")})
        if auto_upload:
            job["auto_upload_started"] = True
            await tb.start_job_upload(app, job, upload_list, auto=True)
        return
    except Exception as exc:
        tb.logger.warning("Could not send final card for #%s: %s", job.get("id"), exc)
        return
    if old:
        try:
            await app.bot.delete_message(chat_id=old["chat_id"], message_id=old["message_id"])
        except Exception as exc:
            tb.logger.debug("Old job card not deleted: %s", exc)
    await tb.update_status_message(app, job["chat_id"], job.get("user_id"))
    if auto_upload:
        job["auto_upload_started"] = True
        await tb.start_job_upload(app, job, upload_list, auto=True)
