"""Job lifecycle actions: auto-upload, restart, delete and job card buttons."""

from __future__ import annotations

import asyncio
import html
import os
import shutil
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import Application, ContextTypes

from app.bot import flood_guard
from app.bot import telegram_bot as tb
from app.bot.icons import ICON_UPLOAD
from app.services import upload_filter


def job_files(job: dict) -> list[str]:
    """Every file a finished job produced (folders expanded), relative to Download/."""
    files: list[str] = []
    for rel in tb.job_outputs(job):
        full = tb.DOWNLOAD_DIR / rel
        files += tb.get_all_files_in_folder(rel) if full.is_dir() else [rel]
    return list(dict.fromkeys(files))


def _only_leftovers(folder: Path) -> bool:
    """True if ``folder`` holds no files the user would see (only dotfiles and the like)."""
    for root, _, names in os.walk(folder):
        for name in names:
            if not tb.is_internal(str((Path(root) / name).relative_to(tb.DOWNLOAD_DIR))):
                return False
    return True


def prune_empty_folders(path: Path) -> list[Path]:
    """Remove the folders above ``path`` that are left empty, up to Download/.

    "Empty" also covers folders with nothing but leftovers (dotfiles). The
    bot's own top-level folders (Telegram, Manga, ...) stay.
    """
    removed = []
    parent = path.parent
    while (
        parent != tb.DOWNLOAD_DIR
        and tb.DOWNLOAD_DIR in parent.parents
        and not (parent.parent == tb.DOWNLOAD_DIR and parent.name in tb.KEPT_FOLDERS)
        and parent.is_dir()
        and tb._only_leftovers(parent)
    ):
        shutil.rmtree(parent)
        removed.append(parent)
        parent = parent.parent
    return removed


def filter_auto_upload_files(job: dict, preset) -> tuple[list[str], int]:
    """Apply the auto-upload filter to a finished job: (files to upload, files deleted).

    Files the filter does not keep are deleted, and so are folders that
    leaves empty. Only for automatic uploads; Upload buttons send everything.
    """
    kept, dropped = upload_filter.split(preset, tb.job_files(job))
    deleted = 0
    for rel in dropped:
        full = tb.DOWNLOAD_DIR / rel
        try:
            full.unlink()
            deleted += 1
        except OSError as exc:
            tb.logger.warning("Auto-upload filter could not delete %s: %s", rel, exc)
            continue
        for folder in tb.prune_empty_folders(full):
            tb.logger.info(
                "Auto-upload filter removed empty folder %s", folder.relative_to(tb.DOWNLOAD_DIR)
            )
    if dropped:
        tb.logger.info(
            "Auto-upload filter %s on job #%s: keeping %d file(s), deleted %d: %s",
            upload_filter.normalize(preset),
            job.get("id"),
            len(kept),
            deleted,
            ", ".join(dropped[:20]) + (" …" if len(dropped) > 20 else ""),
        )
    return kept, deleted


def flood_blocked_alert(chat_id: int) -> str:
    """Pop-up for a tap whose screen can't change because the chat is under flood control."""
    when = flood_guard.lift_text(chat_id) if flood_guard.blocked_for(chat_id) else "a while"
    return (
        f"Telegram blocks this bot from editing messages here until {when}. "
        "Uploads and downloads keep running; typed commands may still get a reply."
    )


async def answer_upload_tap(query) -> None:
    """Answer an Upload tap; while flood control blocks the chat, explain that in an alert.

    Otherwise the message would sit on "Preparing upload..." with no hint why.
    """
    chat_id = query.message.chat_id
    if flood_guard.blocked_for(chat_id):
        await tb.answer_once(query, flood_guard.blocked_notice(chat_id), show_alert=True)
    else:
        await tb.answer_once(query)


async def start_job_upload(
    app: Application, job: dict, files: list[str] | None = None, *, auto: bool = False
):
    """Upload a job's files. ``auto`` marks an automatic upload (GIF previews, folder cleanup)."""
    if files is None:
        files = tb.job_files(job)
    if not files:
        return False
    try:
        msg = await app.bot.send_message(
            chat_id=job["chat_id"],
            text=f"{ICON_UPLOAD} Preparing upload of {len(files)} file(s)...",
        )
        message_id = msg.message_id
    except RetryAfter:
        # Flood control in this chat: upload anyway (it goes through the user
        # account); the summary is posted once the limit lifts.
        message_id = None
    tb.run_in_background(
        tb.upload_files_via_pyrogram(
            app,
            job["chat_id"],
            message_id,
            files,
            title=f"Job #{job['id']}: {tb.shorten(tb.clean_download_name(job.get('name', '')), 60)}",
            user_id=job.get("user_id"),
            auto=auto,
        ),
        name=f"upload-job-{job['id']}",
        on_error=tb.message_error_reporter(app, job["chat_id"], message_id, "Upload"),
    )
    return True


def delete_job_outputs(job: dict) -> int:
    removed = 0
    for rel in tb.job_outputs(job):
        try:
            tb.delete_path(rel)
            removed += 1
        except (OSError, ValueError) as exc:
            tb.logger.warning("Could not delete %s: %s", rel, exc)
        # Drop folders the deletion left empty, up to Download/.
        parent = (tb.DOWNLOAD_DIR / rel).parent
        while parent != tb.DOWNLOAD_DIR and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return removed


async def restart_job(app: Application, job: dict) -> dict | None:
    """Start the same download again (Retry on a failed card)."""
    chat_id, user_id = job["chat_id"], job.get("user_id")
    provider = job.get("provider")
    if provider == "yt-dlp":
        return await tb.start_ytdlp_download(
            app,
            chat_id,
            job["url"],
            audio_only=bool(job.get("audio_only")),
            user_id=user_id,
            run_in_background=True,
            max_height=job.get("max_height"),
            gif=bool(job.get("gif")),
        )
    if provider == "spotify":
        return await tb.start_spotify_download(app, chat_id, job["url"], user_id)
    if provider == "manga":
        return await tb.start_manga_download(app, chat_id, job["url"], user_id)
    if provider == "gallery-dl":
        return await tb.start_gallery_download(
            app, chat_id, job["url"], job.get("category", "gallery"), user_id
        )
    if not provider and job.get("source"):
        return await tb.start_aria2_download(app, chat_id, job["source"], user_id)
    return None


@tb.auto_answer
async def handle_job_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons on job cards and the status dashboard: job:<action>:<id>."""
    query = update.callback_query
    app = context.application
    _, action, raw_id = (query.data.split(":", 2) + ["", ""])[:3]
    job = tb.download_jobs.get(int(raw_id)) if raw_id.isdigit() else None
    if job is None:
        await tb.answer_once(query, "This job is no longer in the list.", show_alert=True)
        return

    def is_card_message() -> bool:
        card = job.get("card") or {}
        return card.get("message_id") == query.message.message_id

    if action in ("pause", "resume"):
        ok, msg = await (tb.pause_job if action == "pause" else tb.resume_job)(job["id"])
        await tb.answer_once(query, msg, show_alert=not ok)
        await tb.refresh_job_card(app, job, force=True)

    elif action == "cc":
        await tb.refresh_job_card(app, job, force=True, confirm="cancel")

    elif action == "cn":
        await tb.refresh_job_card(app, job, force=True)

    elif action == "cy":
        ok, msg = await tb.cancel_job(job["id"])
        await tb.answer_once(query, msg, show_alert=not ok)
        if ok:
            text, markup = tb.render_job_card(job)
            job["card_final"] = job.get("status")
            await tb.safe_edit_message(query.message, text, markup, parse_mode=ParseMode.HTML)
            await tb.update_status_message(app, job["chat_id"], job.get("user_id"))

    elif action == "show":
        # Bring the card down to the bottom of the chat.
        old = job.get("card")
        await tb._send_card(app, job)
        if old:
            try:
                await app.bot.delete_message(chat_id=old["chat_id"], message_id=old["message_id"])
            except Exception as exc:
                tb.logger.debug("Old job card not deleted: %s", exc)

    elif action == "up":
        files = tb.job_files(job)
        if not files:
            await tb.answer_once(
                query, "The downloaded files are no longer there.", show_alert=True
            )
            return
        await tb.answer_upload_tap(query)
        await tb.start_job_upload(app, job, files)

    elif action == "gp":
        source = await asyncio.to_thread(tb.job_preview_video, job)
        if source is None:
            await tb.answer_once(
                query, "There is no video from this job on the server.", show_alert=True
            )
            return
        await tb.answer_once(query, "Making GIF previews…")
        tb.run_in_background(
            tb.send_gif_previews(app, job["chat_id"], source), name=f"gif-previews-{job['id']}"
        )

    elif action in ("del", "deln"):
        text, markup = tb.render_job_card(job, confirm="delete" if action == "del" else None)
        await tb.safe_edit_message(query.message, text, markup, parse_mode=ParseMode.HTML)

    elif action == "dely":
        removed = await asyncio.to_thread(tb.delete_job_outputs, job)
        await tb.answer_once(query, f"Deleted {removed} item(s).")
        await tb.safe_edit_message(
            query.message,
            f"🗑 <b>{html.escape(tb.shorten(tb.clean_download_name(job.get('name', '')), 80))}</b>\n"
            f"#{job['id']} · files deleted from the server",
            None,
            parse_mode=ParseMode.HTML,
        )

    elif action == "retry":
        try:
            new_job = await tb.restart_job(app, job)
        except Exception as exc:
            await tb.answer_once(
                query, f"Couldn't start it again: {tb.shorten(str(exc), 150)}", show_alert=True
            )
            return
        if new_job is None:
            await tb.answer_once(
                query,
                "This kind of download can't be retried; send the link again.",
                show_alert=True,
            )
            return
        await tb.answer_once(query, "Starting again…")
        await tb.attach_job_card(app, new_job, message=query.message if is_card_message() else None)

    elif action == "cmp":
        source = await asyncio.to_thread(tb.job_preview_video, job)
        if source is None:
            await tb.answer_once(
                query, "There is no video from this job on the server.", show_alert=True
            )
            return
        await tb.start_compress_job(
            app, job["chat_id"], job.get("user_id"), tb.file_rel_path(source)
        )

    elif action == "dismiss":
        try:
            await query.message.delete()
        except Exception:
            await tb.safe_edit_message(query.message, "Dismissed.")
