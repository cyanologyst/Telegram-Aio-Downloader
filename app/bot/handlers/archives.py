"""Zip/archive jobs and sending archives to a chat."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from pathlib import Path

from telegram.error import BadRequest
from telegram.ext import Application

from app.bot.icons import ICON_OK, ICON_WARN
from app.bot.views import files as file_views
from app.services.archive import ZipProgress
from app.bot import telegram_bot as tb


def archive_kwargs_from_settings(settings: dict) -> dict:
    password = (settings.get("password") or "").strip() or None
    try:
        compression_level = int(settings.get("compression_level", 5))
    except (TypeError, ValueError):
        compression_level = 5
    compression_level = max(1, min(9, compression_level))
    try:
        max_part_size = int(settings.get("zip_part_size", tb.MAX_ZIP_PART_SIZE))
    except (TypeError, ValueError):
        max_part_size = tb.MAX_ZIP_PART_SIZE
    method = (settings.get("zip_method") or "zip").lower()
    if method not in ("zip", "7z"):
        method = "zip"
    return {
        "password": password,
        "max_part_size": max(1, max_part_size),
        "archive_format": method,
        "compression_level": compression_level,
    }


def file_rel_path(path: Path) -> str:
    return str(path.relative_to(tb.DOWNLOAD_DIR)).replace("\\", "/")


def build_files_to_zip(file_paths: list) -> list:
    return [
        (i + 1, tb.file_rel_path(f), f, f.stat().st_size)
        for i, f in enumerate(file_paths)
    ]


def format_archive_progress_text(progress: ZipProgress, prefix: str = "📦") -> str:
    snap = progress.snapshot()
    stage = snap["stage"]
    if stage == "splitting":
        part_info = ""
        if snap["total_parts"]:
            part_info = f"\nVolume {snap['current_part']}/{snap['total_parts']}"
        return f"{prefix} Splitting archive into volumes...{part_info}"
    total_b = snap["total_bytes"] or 1
    pct = snap["done_bytes"] / total_b * 100
    bar = tb.render_progress_bar(pct)
    part_info = ""
    if snap["total_parts"] > 1:
        part_info = f"\nVolume {snap['current_part']}/{snap['total_parts']}"
    current = snap["current_file"]
    file_line = f"\n{current[:50]}" if current else ""
    return (
        f"{prefix} {stage.title()}...{part_info}\n"
        f"{bar} {pct:.0f}%\n"
        f"Files: {snap['done_files']}/{snap['total_files']}"
        f"{file_line}"
    )


def default_archive_name() -> str:
    return f"archive_{datetime.now().strftime('%Y%m%d_%H%M')}"


def sanitize_archive_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip("._- ")[:100]


async def ask_zip_name(message, user_id: int, chat_id: int, paths: list, *, default_name: str, cancel_data: str):
    """Ask for an archive name (or offer a default) before zipping ``paths``."""
    paths = sorted(paths, key=lambda path: str(path).lower())
    tb.pending_zip_name_sessions[user_id] = {
        "files_to_zip": tb.build_files_to_zip(paths),
        "source_files": paths,
        "settings": tb.get_user_settings(user_id),
        "default_name": tb.sanitize_archive_name(default_name) or tb.default_archive_name(),
        "chat_id": chat_id,
    }
    size = sum(path.stat().st_size for path in paths)
    await tb.show_screen(message, file_views.zip_name_screen(
        len(paths), size, tb.pending_zip_name_sessions[user_id]["default_name"], cancel_data
    ))


async def run_named_zip(app: Application, user_id: int, name: str, status_msg) -> None:
    """Create the archive for a pending zip session and send it to the chat."""
    session = tb.pending_zip_name_sessions.pop(user_id, None)
    if session is None:
        await status_msg.edit_text("This archive request has expired. Start it again from Archive.")
        return
    chat_id = session["chat_id"]
    settings = session["settings"]
    await status_msg.edit_text(f"📦 Creating {name}...")

    async def on_progress(prog_text: str):
        try:
            await status_msg.edit_text(prog_text)
        except BadRequest:
            pass

    sent_while_zipping = []

    upload_callback = tb.create_zip_upload_callback(app, chat_id, user_id, settings, status_msg, sent_while_zipping)
    zip_paths, size_warnings = await tb.run_archive_job(
        user_id,
        session["files_to_zip"],
        tb.DOWNLOAD_DIR,
        zip_name=name,
        settings=settings,
        on_progress=on_progress,
        upload_callback=upload_callback,
    )
    all_ok = True
    if zip_paths:
        all_ok = await tb.send_archives_to_chat(app, chat_id, zip_paths, settings, status_msg, user_id)

    deleted_sources = False
    if all_ok and settings.get("auto_delete_files_after_zip"):
        for path in session["source_files"]:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                tb.logger.warning("Could not delete zipped file %s: %s", path, exc)
        deleted_sources = True

    parts = len(sent_while_zipping) + len(zip_paths)
    lines = [
        f"{ICON_OK if all_ok else ICON_WARN} Archive {'sent' if all_ok else 'partly sent'}: {name}",
        f"Files: {len(session['source_files'])} · parts: {parts}",
    ]
    if sent_while_zipping:
        lines.append("Parts were uploaded as they were created and removed from disk.")
    elif zip_paths and settings.get("auto_delete_zips_after_send") and all_ok:
        lines.append("The archive was removed from disk after sending.")
    elif zip_paths:
        lines.append("The archive is kept in the Download folder.")
    if deleted_sources:
        lines.append("The original files were deleted (Archive settings).")
    if not all_ok:
        lines.append("Some parts failed to send; check the bot log.")
    lines += [f"{ICON_WARN} {warning}" for warning in size_warnings]
    await status_msg.edit_text("\n".join(lines))


async def maybe_delete_file_after_upload(user_id: int, rel_path: str, *, prune: bool = False) -> None:
    """Delete an uploaded file if the user wants that; ``prune`` also drops folders it left empty."""
    if not user_id:
        return
    settings = tb.get_user_settings(user_id)
    if not settings.get("auto_delete_files_after_upload"):
        return
    tb.delete_uploaded_file(rel_path, prune=prune)


def delete_uploaded_file(rel_path: str, *, prune: bool = False) -> None:
    try:
        full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
        if full.is_file():
            full.unlink()
        if prune:
            tb.prune_empty_folders(full)
    except Exception as e:
        tb.logger.warning(f"auto_delete after upload failed for {rel_path}: {e}")


def create_zip_upload_callback(
    context, chat_id: int, user_id: int, settings: dict, status_msg=None, sent: list | None = None
):
    """Upload each archive part as soon as it is written, then delete it.

    The archive is built in a worker thread; this returns a plain function for
    that thread which schedules the upload on the bot's event loop and waits
    for it. The loop is captured here, while running on it: the worker thread
    has no loop of its own, so asking for one there failed and every part
    piled up on disk until the end.
    """
    loop = asyncio.get_running_loop()

    async def upload_and_delete(part_path: Path, current_vol: int, total_vols: int):
        size = part_path.stat().st_size
        caption = f"📦 Part {current_vol}/{total_vols}: {part_path.name}\nSize: {tb.zip_human_size(size)}"
        if total_vols > 1 and current_vol == 1:
            caption += "\nOpen the .001 file with 7-Zip or WinRAR to extract everything."
        if status_msg is not None:
            try:
                await status_msg.edit_text(
                    f"📤 Sending part {current_vol}/{total_vols}: {part_path.name}\n"
                    "Each part is removed from disk once it has been sent."
                )
            except Exception as exc:
                tb.logger.debug("Progress update failed: %s", exc)
        if not await tb.send_archive_document(context, chat_id, part_path, caption):
            return False, f"Failed to send part {current_vol}"
        part_path.unlink(missing_ok=True)
        if sent is not None:
            sent.append(part_path.name)
        return True, None

    def sync_upload_callback(part_path: Path, current_vol: int, total_vols: int) -> tuple:
        future = asyncio.run_coroutine_threadsafe(
            upload_and_delete(part_path, current_vol, total_vols), loop
        )
        try:
            # Big parts take a while; never give up on one that is still sending.
            return future.result()
        except Exception as exc:
            tb.logger.error("Archive part upload failed: %s", exc)
            return False, str(exc)

    return sync_upload_callback


async def run_archive_job(
    user_id: int,
    files_to_zip: list,
    output_dir: Path,
    zip_name: str,
    settings: dict,
    on_progress=None,
    upload_callback=None,
) -> tuple:
    if not files_to_zip:
        raise RuntimeError("No files to archive")

    kwargs = tb.archive_kwargs_from_settings(settings)
    password = kwargs.pop("password")
    warnings = tb.get_oversized_file_warnings(files_to_zip, kwargs["max_part_size"])

    fmt_err = tb.check_archive_format_support(kwargs["archive_format"])
    if fmt_err:
        raise RuntimeError(fmt_err)
    pwd_err = tb.check_password_support(password, kwargs["archive_format"])
    if pwd_err:
        raise RuntimeError(pwd_err)

    progress = ZipProgress()
    loop = asyncio.get_running_loop()

    def worker():
        return tb.make_archive_with_progress(
            files_to_zip,
            output_dir,
            zip_name=zip_name,
            password=password,
            progress=progress,
            on_volume_created=upload_callback,
            **kwargs,
        )

    async with tb.ZIP_LOCKS[user_id]:
        # Use dedicated zip_executor for CPU-intensive compression
        # This prevents the default thread pool from being exhausted
        executor = tb.zip_executor if tb.zip_executor else None
        task = loop.run_in_executor(executor, worker)
        
        # Update progress every 1 second instead of 2 for more responsive feedback
        while not task.done():
            if on_progress:
                try:
                    await on_progress(tb.format_archive_progress_text(progress))
                except BadRequest as e:
                    if "message is not modified" not in str(e).lower():
                        tb.logger.debug(f"Progress update skipped: {e}")
                except Exception as e:
                    tb.logger.debug(f"Progress update failed: {e}")
            await asyncio.sleep(1.0)  # More frequent updates
        
        if on_progress:
            try:
                await on_progress(tb.format_archive_progress_text(progress))
            except Exception:
                pass
        
        paths = await task
        # With an upload callback every part may already be sent and deleted.
        if not paths and upload_callback is None:
            raise RuntimeError("No archives were created")
        return paths, warnings


async def send_archive_document(context, chat_id: int, archive_path: Path, caption: str) -> bool:
    """Send an archive via Bot API or Pyrogram depending on size."""
    size = archive_path.stat().st_size
    try:
        if size <= tb.BOT_MAX_DOCUMENT_BYTES:
            with open(archive_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=caption,
                )
        else:
            client = await tb.get_pyrogram_client()
            await tb.send_with_flood_wait_handling(
                lambda: client.send_document(
                    chat_id=chat_id,
                    document=str(archive_path),
                    caption=caption,
                )
            )
        return True
    except Exception as e:
        tb.logger.error(f"Error sending archive {archive_path.name}: {e}")
        return False


async def send_archives_to_chat(context, chat_id: int, zip_paths: list, settings: dict, status_msg=None, user_id: int = None) -> bool:
    """Send all archives with upload progress; delete each only after successful send. Returns True if all succeeded."""
    all_ok = True
    total = len(zip_paths)
    
    # Calculate total size for progress tracking
    total_size = sum(p.stat().st_size for p in zip_paths)
    sent_size = 0
    
    for i, archive_path in enumerate(zip_paths, 1):
        size = archive_path.stat().st_size
        via = "bot" if size <= tb.BOT_MAX_DOCUMENT_BYTES else "pyrogram"
        caption = f"📦 Volume {i}/{total}: {archive_path.name}\nSize: {tb.zip_human_size(size)}"
        if total > 1 and i == 1:
            caption += "\nOpen the .001 file in WinRAR or 7-Zip to extract everything."
        if via == "pyrogram":
            caption += "\n(Large volume sent via Pyrogram)"
        
        # Update progress message before sending
        if status_msg and user_id:
            try:
                pct = int((sent_size / total_size * 100)) if total_size > 0 else 0
                bar = tb.render_progress_bar(pct)
                progress_text = (
                    f"📤 {tb.get_lang(user_id, 'uploading_volume').format(i, total, archive_path.name)}\n"
                    f"{bar} {pct}% ({tb.zip_human_size(sent_size)}/{tb.zip_human_size(total_size)})\n\n"
                    f"Status: Uploading..."
                )
                await status_msg.edit_text(progress_text)
            except Exception as e:
                tb.logger.debug(f"Progress update failed: {e}")
        
        # Send the archive
        ok = await tb.send_archive_document(context, chat_id, archive_path, caption)
        if not ok:
            all_ok = False
            continue
        
        sent_size += size
        
        # Delete after successful send
        if settings.get("auto_delete_zips_after_send"):
            try:
                archive_path.unlink()
            except Exception as e:
                tb.logger.warning(f"Could not delete archive {archive_path}: {e}")
    
    return all_ok
