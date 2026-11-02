"""Video compression jobs."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import Application, ContextTypes

from app.bot.icons import ICON_UPLOAD
from app.bot.views import compress as compress_views
from app.services import video_compress, video_split
from app.bot import telegram_bot as tb


def _compress_lock() -> asyncio.Lock:
    """One encode at a time: x265 keeps every core busy on its own."""
    loop = asyncio.get_running_loop()
    lock = tb._compress_locks.get(loop)
    if lock is None:
        lock = tb._compress_locks[loop] = asyncio.Lock()
    return lock


async def _run_ffmpeg_progress(cmd: list[str], duration: float | None, progress_callback=None, on_process=None):
    """Run an ffmpeg command that writes ``-progress pipe:1``: (return code, stderr).

    ``progress_callback(pct)`` gets every progress step; ``on_process(proc)``
    receives the process so it can be killed. Cancelling kills ffmpeg.
    """
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    if on_process:
        on_process(process)
    stderr_task = asyncio.create_task(process.stderr.read())
    try:
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            seconds = tb.parse_ffmpeg_progress_seconds(raw.decode("utf-8", errors="ignore"))
            if progress_callback and duration and seconds is not None:
                await progress_callback(max(0, min(99, int(seconds / duration * 100))))
        await process.wait()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        stderr_task.cancel()
        raise
    stderr = (await stderr_task).decode("utf-8", errors="ignore").strip()
    return process.returncode, stderr


async def compress_video(
    source: Path, target: Path, *, crf: int, preset: str, progress_callback=None, on_process=None
) -> str:
    """Re-encode ``source`` into ``target`` (MP4); returns the encoder used.

    Audio is copied when MP4 can hold it; if that still fails, the encode is
    retried with AAC audio. The file is written under a hidden name and
    renamed at the end, so a half-done encode never shows up in Files.
    """
    encoder = await asyncio.to_thread(video_compress.pick_encoder, tb.FFMPEG_BIN)
    info = await asyncio.to_thread(video_split.probe, source, tb.FFPROBE_BIN)
    codecs = await asyncio.to_thread(video_compress.probe_audio_codecs, source, tb.FFPROBE_BIN)
    tmp = target.with_name(f".compressing_{target.name}")
    stderr = ""
    try:
        for copy_audio in ([True, False] if video_compress.can_copy_audio(codecs) else [False]):
            cmd = video_compress.build_compress_command(
                tb.FFMPEG_BIN, source, tmp, encoder=encoder, crf=crf, preset=preset, copy_audio=copy_audio
            )
            returncode, stderr = await tb._run_ffmpeg_progress(
                cmd, info.duration, progress_callback, on_process
            )
            if returncode == 0:
                break
            if returncode < 0:  # killed: the user cancelled
                raise RuntimeError("ffmpeg was stopped")
            tb.logger.warning("Compressing %s failed (copy audio=%s): %s", source, copy_audio, stderr[-300:])
        else:
            raise RuntimeError(f"ffmpeg failed: {stderr[-300:] or 'unknown error'}")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return encoder


def new_compress_job(chat_id: int, message_id: int, user_id: int | None, rel_path: str, back: str | None) -> dict:
    tb._compress_counter += 1
    settings = tb.get_user_settings(user_id or 0)
    encoder = video_compress.pick_encoder(tb.FFMPEG_BIN)
    job = {
        "id": str(tb._compress_counter),
        "chat_id": chat_id,
        "message_id": message_id,
        "user_id": user_id,
        "source": rel_path,
        "output": None,
        "codec": video_compress.codec_label(encoder),
        "crf": video_compress.normalize_crf(settings.get("compress_crf")),
        "preset": video_compress.normalize_preset(settings.get("compress_preset")),
        "status": "queued",
        "pct": 0,
        "back": back,
        "last_edit": 0.0,
    }
    tb.compress_jobs[job["id"]] = job
    return job


def active_compress_for(rel_path: str) -> dict | None:
    return next(
        (j for j in tb.compress_jobs.values()
         if j["source"] == rel_path and j["status"] in ("queued", "encoding")),
        None,
    )


async def refresh_compress_card(app: Application, job: dict, *, force: bool = False, confirm_replace: bool = False):
    if not tb.edit_throttle.due(
        job, ("compress", job["id"]), job.get("pct"), phase=job.get("status"), force=force
    ):
        return
    job["last_edit"] = time.time()
    text, markup = compress_views.compress_card(job, tb.encode_path, confirm_replace=confirm_replace)
    try:
        await tb._edit_dashboard_message(
            app, job["chat_id"], job["message_id"], text, markup, parse_mode=ParseMode.HTML
        )
    except tb._DashboardMessageGone:
        msg = await app.bot.send_message(
            chat_id=job["chat_id"], text=text, reply_markup=markup,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
        job["message_id"] = msg.message_id


async def run_compress_job(app: Application, job: dict) -> None:
    """Wait for the encoder, re-encode the video and keep the card up to date."""
    source = tb.safe_join(tb.DOWNLOAD_DIR, job["source"])
    try:
        async with tb._compress_lock():
            if job["status"] == "cancelled":
                return
            if not source.is_file():
                raise FileNotFoundError(f"{source.name} is no longer on the server.")
            job.update(status="encoding", started_at=time.time(), original_size=source.stat().st_size)
            await tb.refresh_compress_card(app, job, force=True)

            async def progress(pct: int):
                job["pct"] = pct
                await tb.refresh_compress_card(app, job)

            def remember(process):
                job["process"] = process

            target = video_compress.output_path(source, video_compress.pick_encoder(tb.FFMPEG_BIN))
            encoder = await tb.compress_video(
                source, target, crf=job["crf"], preset=job["preset"],
                progress_callback=progress, on_process=remember,
            )
            job.pop("process", None)
            if job["status"] == "cancelled":
                target.unlink(missing_ok=True)
                return
            job.update(
                status="done",
                pct=100,
                finished_at=time.time(),
                codec=video_compress.codec_label(encoder),
                output=tb.file_rel_path(target),
                new_size=target.stat().st_size,
            )
    except Exception as exc:
        job.pop("process", None)
        if job["status"] != "cancelled":
            tb.logger.warning("Compressing %s failed: %s", job["source"], exc)
            job.update(status="failed", error=tb.shorten(str(exc), 300), finished_at=time.time())
    await tb.refresh_compress_card(app, job, force=True)


async def start_compress_job(
    app: Application, chat_id: int, user_id: int | None, rel_path: str, *, message=None, back: str | None = None
) -> dict:
    """Start compressing ``rel_path``. Its card replaces ``message``, or is sent as a new one."""
    running = tb.active_compress_for(rel_path)
    if running:
        await tb.refresh_compress_card(app, running, force=True)
        return running
    if message is None:
        message = await app.bot.send_message(chat_id=chat_id, text="🗜 Preparing compression…")
    job = tb.new_compress_job(chat_id, message.message_id, user_id, rel_path, back)
    await tb.refresh_compress_card(app, job, force=True)
    tb.run_in_background(tb.run_compress_job(app, job), name=f"compress-{job['id']}")
    return job


def cancel_compress_job(job: dict) -> bool:
    if job["status"] not in ("queued", "encoding"):
        return False
    job["status"] = "cancelled"
    process = job.get("process")
    if process is not None and process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    return True


def replace_with_compressed(job: dict) -> str:
    """Delete the original and give the compressed file its name; returns the new path."""
    source = tb.safe_join(tb.DOWNLOAD_DIR, job["source"])
    output = tb.safe_join(tb.DOWNLOAD_DIR, job["output"])
    if not output.is_file():
        raise FileNotFoundError(f"{output.name} is no longer on the server.")
    final = source.with_suffix(".mp4")
    source.unlink(missing_ok=True)
    if final.exists():
        # Another file already has that name (e.g. an .mkv beside an .mp4 of
        # the same name): keep the compressed file's own name.
        final = output
    else:
        output.replace(final)
    job.update(status="replaced", source=tb.file_rel_path(final), output=tb.file_rel_path(final))
    return job["output"]


@tb.auto_answer
async def handle_compress_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons on compress cards: cmp:<action>:<id>."""
    query = update.callback_query
    app = context.application
    _, action, cid = (query.data.split(":", 2) + ["", ""])[:3]
    job = tb.compress_jobs.get(cid)
    if job is None:
        await tb.answer_once(query, "This compression is no longer in the list.", show_alert=True)
        return
    job["message_id"] = query.message.message_id

    if action == "cancel":
        if tb.cancel_compress_job(job):
            await tb.answer_once(query, "Cancelling…")
        await tb.refresh_compress_card(app, job, force=True)

    elif action == "up":
        if not job.get("output") or not tb.safe_join(tb.DOWNLOAD_DIR, job["output"]).is_file():
            await tb.answer_once(query, "The compressed file is no longer there.", show_alert=True)
            return
        await tb.answer_upload_tap(query)
        try:
            msg = await app.bot.send_message(chat_id=job["chat_id"], text=f"{ICON_UPLOAD} Preparing upload...")
            message_id = msg.message_id
        except RetryAfter:
            message_id = None  # flood control: upload anyway, the result is held
        tb.run_in_background(
            tb.send_single_file_via_pyrogram(app, job["chat_id"], message_id, job["output"], user_id=job.get("user_id")),
            name="upload",
            on_error=tb.message_error_reporter(app, job["chat_id"], message_id, "Upload"),
        )

    elif action in ("rep", "repn") and job["status"] == "done":
        await tb.refresh_compress_card(app, job, force=True, confirm_replace=action == "rep")

    elif action == "repy" and job["status"] == "done":
        try:
            await asyncio.to_thread(tb.replace_with_compressed, job)
        except OSError as exc:
            await tb.answer_once(query, f"Couldn't replace it: {tb.shorten(str(exc), 150)}", show_alert=True)
            return
        await tb.answer_once(query, "Replaced the original.")
        await tb.refresh_compress_card(app, job, force=True)
