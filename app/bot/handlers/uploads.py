"""Upload jobs, progress cards and held (flood-blocked) messages."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time

from pyrogram import StopTransmission
from pyrogram.errors import RPCError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter
from telegram.ext import Application

from app.bot import flood_guard
from app.bot import telegram_bot as tb
from app.bot.icons import ICON_BOX, ICON_FOLDER, ICON_OK, ICON_STOP, ICON_UPLOAD


def upload_progress_markup(upload_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{ICON_STOP} Cancel upload", callback_data=f"up_cancel:{upload_id}"
                )
            ]
        ]
    )


def upload_done_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{ICON_FOLDER} Files", callback_data="fb:list:0:")]]
    )


def new_upload_job(
    chat_id: int,
    message_id: int,
    files: list,
    target: int | str = "me",
    target_label: str = "Saved Messages",
) -> str:
    tb.upload_counter += 1
    upload_id = f"upload_{tb.upload_counter}"
    tb.upload_jobs[upload_id] = {
        "id": upload_id,
        "status": "uploading",
        "files": list(files),
        "current_file": 0,
        "chat_id": chat_id,
        "message_id": message_id,
        "sent_count": 0,
        "last_update": 0,
        "cancelled": False,
        "target": target,
        "target_label": target_label,
    }
    # Finished uploads are kept for the dashboard, but not for ever.
    finished = [uid for uid, j in tb.upload_jobs.items() if j.get("status") != "uploading"]
    for uid in finished[: max(0, len(finished) - tb.MAX_FINISHED_UPLOADS)]:
        tb.upload_jobs.pop(uid, None)
    return upload_id


def cancel_upload(upload_id: str) -> bool:
    job = tb.upload_jobs.get(upload_id)
    if not job or job.get("status") != "uploading":
        return False
    job["cancelled"] = True
    return True


async def update_upload_progress(
    app: Application,
    chat_id: int,
    message_id: int,
    upload_id: str,
    file_label: str,
    sent: int,
    total: int,
):
    job = tb.upload_jobs.get(upload_id)
    if job is None:
        return
    if job.get("cancelled"):
        # Raised inside Pyrogram's progress callback, this aborts the transfer
        # (the same thing Client.stop_transmission() does).
        raise StopTransmission()

    pct = sent * 100.0 / total if total else None
    if not tb.edit_throttle.due(
        job, ("upload", upload_id), pct, phase=file_label, final=sent >= total
    ):
        return
    job["last_update"] = time.time()

    text = (
        f"{ICON_UPLOAD} Uploading to {job.get('target_label', 'Saved Messages')}\n\n"
        f"{file_label}\n"
        f"{tb.build_progress_bar(sent, total, width=15)}\n"
        f"{ICON_BOX} {tb.human_size(sent)} / {tb.human_size(total)}"
    )
    try:
        await tb._edit_dashboard_message(
            app, chat_id, message_id, text, tb.upload_progress_markup(upload_id)
        )
    except tb._DashboardMessageGone:
        tb.logger.warning("Upload progress message %s no longer exists", message_id)
    except Exception as exc:
        tb.logger.debug("Upload progress edit failed: %s", exc)


async def _send_via_bot_api(app: Application, job: dict, rel_path: str) -> None:
    """Bot-only mode: send a file of up to 50 MB through the Bot API.

    It goes to the upload target when one is set, otherwise to the chat.
    """
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    size = full.stat().st_size
    if size > tb.BOT_MAX_DOCUMENT_BYTES:
        raise RuntimeError(
            f"{full.name} is {tb.human_size(size)}. Without the Pyrogram login only files up to "
            "50 MB can be sent; set up the login to upload bigger files."
        )
    target = job.get("target", "me")
    with open(full, "rb") as fh:  # noqa: ASYNC230
        await app.bot.send_document(
            chat_id=job["chat_id"] if target == "me" else target,
            document=fh,
            caption=tb.build_upload_caption(rel_path),
            read_timeout=300,
            write_timeout=300,
        )
    job["via_bot"] = True


async def _upload_one_file(app: Application, upload_id: str, rel_path: str, label: str) -> bool:
    """Upload one file (split into parts above the size limit). False if cancelled."""
    job = tb.upload_jobs[upload_id]
    try:
        await tb.get_pyrogram_client()
    except tb.PyrogramUnavailable:
        await tb._send_via_bot_api(app, job, rel_path)
        return True
    client = await tb.get_pyrogram_client()
    cap = await tb.upload_part_cap(client)
    if tb.safe_join(tb.DOWNLOAD_DIR, rel_path).stat().st_size > cap:
        try:
            await tb._edit_dashboard_message(
                app,
                job["chat_id"],
                job["message_id"],
                f"{ICON_UPLOAD} Uploading to {job.get('target_label', 'Saved Messages')}\n\n"
                f"{label}\n✂️ Splitting into parts of up to {tb.human_size(cap)}…",
                tb.upload_progress_markup(upload_id),
            )
        except Exception as exc:
            tb.logger.debug("Upload split notice not shown: %s", exc)
    chunks, as_document = await asyncio.to_thread(tb.prepare_upload_parts, rel_path, cap)
    try:
        for chunk_path, part, parts in chunks:
            if job["cancelled"]:
                return False
            part_label = f"{label} (part {part}/{parts})" if parts > 1 else label

            async def progress(current, total, part_label=part_label):
                await tb.update_upload_progress(
                    app, job["chat_id"], job["message_id"], upload_id, part_label, current, total
                )

            try:
                await tb.pyrogram_send_file(
                    chunk_path,
                    progress_callback=progress,
                    chat_id=job["target"],
                    force_document=as_document,
                    max_size=cap,
                )
            except StopTransmission:
                return False
            except RPCError as exc:
                tb._raise_if_bot_session(exc)
                raise
            # Most send_* methods swallow StopTransmission and just return.
            if job["cancelled"]:
                return False
    finally:
        # Parts are a full second copy of the file on disk.
        tb.cleanup_file_chunks(chunks)
    return True


async def upload_files_via_pyrogram(
    app: Application,
    chat_id: int,
    message_id: int,
    files: list,
    *,
    title: str,
    user_id: int = None,
    auto: bool = False,
) -> dict:
    """Upload ``files`` (paths relative to Download/) to the user's upload target.

    That is Saved Messages unless a channel or group is set in Settings.
    Progress is shown on ``message_id`` with a Cancel button; the message ends
    as a summary. Returns the upload job. Each video is followed by its GIF
    preview in the same chat (unless turned off in Settings), for automatic
    and manual uploads alike. ``auto`` (an automatic upload) also drops
    folders emptied by deleting.
    """
    if not files:
        raise RuntimeError("No files to upload.")
    settings = tb.get_user_settings(user_id or 0)
    upload_id = tb.new_upload_job(
        chat_id, message_id, files, tb.upload_target(settings), tb.upload_target_label(settings)
    )
    job = tb.upload_jobs[upload_id]
    total_files = len(files)
    try:
        for idx, rel in enumerate(files, start=1):
            if job["cancelled"]:
                break
            job["current_file"] = idx
            name = os.path.basename(rel)
            label = f"File {idx}/{total_files}: {name}" if total_files > 1 else name
            refusal = await tb.upload_refusal(rel)
            if refusal:
                # Sending a half-downloaded file gives a broken video; the file
                # is kept (not deleted) so it can be uploaded once it's done.
                tb.logger.info("Upload of %s refused: %s", rel, refusal)
                job.setdefault("refused", []).append((name, refusal))
                continue
            chain = tb._send_chain(chat_id, job["target"])
            if chain.locked():
                # Without this the message sits on "Preparing upload..." for as
                # long as another upload (and its preview) takes.
                with contextlib.suppress(tb._DashboardMessageGone):
                    await tb._edit_dashboard_message(
                        app,
                        chat_id,
                        message_id,
                        f"{ICON_UPLOAD} Waiting for the upload ahead to finish...\n\n{label}",
                        tb.upload_progress_markup(upload_id),
                    )
            async with chain:
                if not await tb._upload_one_file(app, upload_id, rel, label):
                    break
                job["sent_count"] += 1
                if tb.wants_upload_previews(user_id, rel):
                    await tb.send_upload_previews(
                        app,
                        chat_id,
                        job["target"],
                        rel,
                        delete_after=bool(
                            user_id and settings.get("auto_delete_files_after_upload")
                        ),
                        prune=auto,
                    )
                else:
                    await tb.maybe_delete_file_after_upload(user_id, rel, prune=auto)
    except Exception:
        job["status"] = "failed"
        raise

    sent = job["sent_count"]
    refused = job.get("refused") or []
    if job["cancelled"]:
        job["status"] = "cancelled"
        text = (
            f"{ICON_STOP} Upload cancelled\n\n{title}\n"
            f"Uploaded before cancelling: {sent}/{total_files}"
        )
    elif refused and not sent:
        job["status"] = "refused"
        text = f"⏳ Upload refused\n\n{title}"
        if total_files > 1:
            text += f"\nFiles: 0/{total_files}"
    else:
        job["status"] = "completed"
        destination = (
            "this chat" if job.get("via_bot") and job["target"] == "me" else job["target_label"]
        )
        text = f"{ICON_OK} Upload complete\n\n{title}\nSent to: {destination}"
        if total_files > 1:
            text += f"\nFiles: {sent}/{total_files}"
    if refused:
        text += "\n\n" + "\n".join(f"⏳ {name}: {reason}" for name, reason in refused[:10])
        if len(refused) > 10:
            text += f"\n…and {len(refused) - 10} more"
    await tb._show_upload_result(app, chat_id, message_id, text)
    return job


async def _show_upload_result(
    app: Application, chat_id: int, message_id: int | None, text: str
) -> None:
    """Show an upload's summary on its message, or a new one if there is none.

    While Telegram's flood control blocks the chat the summary is held and
    shown once it lifts (``hold_message``), so the message does not stay on
    "Preparing upload..." or a stale progress bar for good.
    """
    await tb.show_or_hold(app, chat_id, message_id, text, "upload_done")


async def show_or_hold(
    app: Application, chat_id: int, message_id: int | None, text: str, buttons: str | None = None
) -> None:
    """Turn ``message_id`` into ``text`` (or post it new); while the chat is blocked, hold it."""
    if flood_guard.can_send(chat_id) and await tb._post_message_now(
        app, chat_id, message_id, text, buttons
    ):
        return
    if flood_guard.blocked_for(chat_id):
        tb.hold_message(app, chat_id, {"text": text, "message_id": message_id, "buttons": buttons})


async def _post_message_now(
    app: Application, chat_id: int, message_id: int | None, text: str, buttons: str | None
) -> bool:
    """Edit ``message_id`` to ``text``, or send it new. False if Telegram refused (flood control)."""
    markup = tb._HELD_MARKUPS[buttons]() if buttons else None
    if message_id:
        try:
            if await tb._edit_dashboard_message(app, chat_id, message_id, text, markup):
                return True
            if not flood_guard.can_send(chat_id):
                return False
            # Edit refused (by flood control, which may still let a new message
            # through, or for another reason): the result must still be seen.
        except tb._DashboardMessageGone:
            pass
    try:
        await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        return True
    except RetryAfter as exc:
        flood_guard.note(chat_id, tb._retry_seconds(exc))
        return False


def _retry_seconds(exc: RetryAfter) -> float:
    retry_after = getattr(exc, "retry_after", 5) or 5
    if hasattr(retry_after, "total_seconds"):
        retry_after = retry_after.total_seconds()
    return float(retry_after)


def hold_message(app: Application, chat_id: int, item: dict) -> None:
    """Post ``item`` in ``chat_id`` once its flood control lifts (kept across restarts)."""
    flood_guard.hold(chat_id, item)
    tb.logger.info(
        "Message for chat %s held until flood control lifts (%s)",
        chat_id,
        flood_guard.lift_text(chat_id),
    )
    tb.schedule_held_flush(app, chat_id)
    tb.run_in_background(tb.announce_flood_block(chat_id), name=f"flood-notice-{chat_id}")


def schedule_held_flush(app: Application, chat_id: int) -> None:
    """Start the one task per chat that posts its held messages when the block lifts."""
    task = tb._held_flushers.get(chat_id)
    if task is not None and not task.done():
        return
    tb._held_flushers[chat_id] = tb.run_in_background(
        tb._flush_held_messages(app, chat_id), name=f"held-messages-{chat_id}"
    )


async def _flush_held_messages(app: Application, chat_id: int) -> None:
    """Wait out the block, then post the held messages one by one (oldest first).

    A 429 on the way just waits for the new block; each message gets a few
    attempts, so a message Telegram never accepts can't loop for ever.
    """
    attempts: dict[int, int] = {}
    while True:
        items = flood_guard.held(chat_id)
        if not items:
            return
        remaining = flood_guard.blocked_for(chat_id)
        if remaining:
            # Telegram may take new messages before the block ends (a reply
            # proved it): post plain results as new messages then. Job cards
            # are edits, so they wait for the end.
            items = (
                [i for i in items if i.get("job_id") is None]
                if flood_guard.sends_open(chat_id)
                else []
            )
            if not items:
                await asyncio.sleep(min(remaining + 5, tb.HELD_RECHECK))
                continue
        item = items[0]
        key = id(item)
        try:
            if item.get("job_id") is not None:
                job = tb.download_jobs.get(item["job_id"])
                posted = True
                if job is not None:
                    await tb.finish_job_card(app, job)
                    posted = job.get("card_final") == job.get("status")
            else:
                posted = await tb._post_message_now(
                    app, chat_id, item.get("message_id"), item.get("text", ""), item.get("buttons")
                )
        except Exception:
            tb.logger.exception("Posting a held message in chat %s failed", chat_id)
            posted = False
        if not remaining or flood_guard.sends_open(chat_id):
            # A refusal by the still-running block is no reason to give up.
            attempts[key] = attempts.get(key, 0) + 1
        if posted or attempts.get(key, 0) >= tb.HELD_MAX_ATTEMPTS:
            flood_guard.release(chat_id, item)
        await asyncio.sleep(tb.HELD_SPACING)


async def announce_flood_block(chat_id: int) -> None:
    """Once per block, tell the user in Saved Messages why the bot went quiet.

    The bot itself can't post in its chat, so without this nothing at all
    explains why messages and commands get no answer. Only the user account
    (Pyrogram) can say it, and only to its own Saved Messages, so this is done
    only when the blocked chat is that user's private chat with the bot.
    """
    if not flood_guard.blocked_for(chat_id):
        return
    try:
        client = await tb.get_pyrogram_client()
        me = getattr(client, "me", None) or await client.get_me()
    except Exception as exc:
        tb.logger.debug("No user account to report the flood block with: %s", exc)
        return
    if getattr(me, "id", None) != chat_id or not flood_guard.take_announcement(chat_id):
        return
    try:
        await client.send_message(
            "me",
            "⚠️ Telegram's flood control blocks the bot from posting or editing messages "
            f"in its chat until {flood_guard.lift_text(chat_id)}.\n\n"
            "Downloads and uploads keep running, and uploads still arrive here. Results "
            "the bot could not post are shown in its chat when the block lifts. Buttons "
            "still answer with a pop-up. Telegram sometimes takes new messages during a block, "
            "so the bot tries a reply at most once a minute; if a command gets no answer, "
            "try again a minute later.",
        )
    except Exception as exc:
        tb.logger.warning("Could not post the flood-control notice in Saved Messages: %s", exc)


async def send_single_file_via_pyrogram(
    app: Application,
    chat_id: int,
    message_id: int,
    rel_path: str,
    user_id: int = None,
):
    return await tb.upload_files_via_pyrogram(
        app,
        chat_id,
        message_id,
        [rel_path],
        title=f"Name: {os.path.basename(rel_path)}",
        user_id=user_id,
    )


async def send_folder_files_via_pyrogram(
    app: Application,
    chat_id: int,
    message_id: int,
    rel_path: str,
    file_list: list = None,
    user_id: int = None,
):
    """Upload every file in a folder, or only ``file_list`` when given."""
    files = tb.get_all_files_in_folder(rel_path) if file_list is None else file_list
    title = (
        f"Folder: /{rel_path.lstrip('/')}"
        if file_list is None
        else f"{len(files)} selected file(s)"
    )
    return await tb.upload_files_via_pyrogram(
        app, chat_id, message_id, files, title=title, user_id=user_id
    )


async def upload_mini_app_selection(
    app: Application,
    chat_id: int,
    files: list[str],
    user_id: int = None,
):
    if not files:
        raise RuntimeError("No files selected.")

    try:
        status_msg = await app.bot.send_message(
            chat_id=chat_id,
            text=f"{ICON_UPLOAD} Preparing mini-app upload ({len(files)} files)...",
        )
        message_id = status_msg.message_id
    except RetryAfter:
        message_id = None  # flood control: upload anyway, the result is held
    # In the background: the web request waits only 30 s for this, and an
    # upload taking longer used to come back as an error, inviting a second
    # (duplicate) upload while the first was still running.
    tb.run_in_background(
        tb.send_folder_files_via_pyrogram(
            app, chat_id, message_id, "", file_list=files, user_id=user_id
        ),
        name="upload-mini-app",
        on_error=tb.message_error_reporter(app, chat_id, message_id, "Upload"),
    )
    return f"Queued upload for {len(files)} file(s)."


async def zip_upload_mini_app_selection(
    app: Application,
    chat_id: int,
    files: list[str],
    user_id: int,
    job_id: str,
):
    job = tb.mini_app_zip_jobs[job_id]
    try:
        settings = tb.get_user_settings(user_id)
        source_paths = [tb.safe_join(tb.DOWNLOAD_DIR, rel_path) for rel_path in files]
        source_paths = tb.filter_files_for_archiving(source_paths)
        if not source_paths:
            raise RuntimeError("No files selected.")

        files_to_zip = tb.build_files_to_zip(source_paths)
        zip_name = f"miniapp_{int(time.time())}"
        job.update(
            {
                "status": "zipping",
                "phase": "zipping",
                "progress_text": f"Preparing {len(files_to_zip)} file(s)...",
                "file_count": len(files_to_zip),
                "created": [],
            }
        )

        async def on_progress(text: str):
            job["progress_text"] = tb.clean_emoji_prefix(text)
            job["updated_at"] = tb.now_ts()

        zip_paths, size_warnings = await tb.run_archive_job(
            user_id,
            files_to_zip,
            tb.DOWNLOAD_DIR,
            zip_name=zip_name,
            settings=settings,
            on_progress=on_progress,
        )

        job["phase"] = "uploading"
        job["status"] = "uploading"
        job["progress_text"] = f"Uploading {len(zip_paths)} archive volume(s)..."
        job["created"] = [p.name for p in zip_paths]
        job["updated_at"] = tb.now_ts()

        all_ok = await tb.send_archives_to_chat(app, chat_id, zip_paths, settings, None, user_id)

        if settings.get("auto_delete_files_after_zip"):
            for path in source_paths:
                try:
                    if path.is_file():
                        path.unlink()
                except Exception as exc:
                    tb.logger.warning("Could not delete zipped source file %s: %s", path, exc)

        job["status"] = "completed" if all_ok else "failed"
        job["phase"] = "completed" if all_ok else "failed"
        job["progress_text"] = (
            "ZIP created and uploaded." if all_ok else "Some archive volumes failed to upload."
        )
        if size_warnings:
            job["warnings"] = size_warnings
        job["finished_at"] = tb.now_ts()
    except Exception as exc:
        job["status"] = "failed"
        job["phase"] = "failed"
        job["progress_text"] = str(exc)
        job["finished_at"] = tb.now_ts()
        tb.logger.error("Mini-app zip job failed: %s", exc)
