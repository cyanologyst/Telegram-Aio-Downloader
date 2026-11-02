"""Playlist downloads and link-request routing."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes

from app.bot import telegram_bot as tb
from app.bot.icons import ICON_STOP
from app.services.hentai_playlist import HentaiPlaylist
from app.services.pornhub_model import PornHubModelPlaylist


async def start_hentai_playlist_download(
    app: Application,
    chat_id: int,
    playlist: HentaiPlaylist,
    user_id: int = None,
):

    async with tb.jobs_lock:
        tb.job_counter += 1
        job_id = tb.job_counter

    episode_count = len(playlist.urls)
    batch_mode = tb.normalize_batch_download_mode(
        tb.get_user_settings(user_id or 0).get("batch_download_mode")
    )
    job = {
        "id": job_id,
        "name": playlist.title,
        "url": playlist.urls[0] if playlist.urls else "",
        "chat_id": chat_id,
        "user_id": user_id or 0,
        "provider": "hentai-playlist",
        "source_type": "hentai_playlist",
        "platform": playlist.site,
        "pid": None,
        "process": None,
        "status": "starting",
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "upload_speed": 0,
        "eta": "Unknown",
        "started_at": tb.now_ts(),
        "finished_at": None,
        "last_line": "Preparing playlist...",
        "episode_count": episode_count,
        "completed_items": 0,
        "uploaded_items": 0,
        "deleted_items": 0,
        "batch_download_mode": batch_mode.value,
        "folder": str(tb.HENTAI_VIDEO_DIR),
    }
    tb.download_jobs[job_id] = job

    async def run_playlist():
        try:
            job["status"] = "downloading"
            await tb.maybe_auto_update_status_message(app, job, force=True)
            await tb.run_video_batch(
                app,
                chat_id,
                playlist.urls,
                job,
                item_label="Episode",
                user_id=user_id,
            )
            if job.get("status") == "cancelled":
                return

            job["status"] = "completed"
            job["progress"] = 100.0
            job["finished_at"] = tb.now_ts()
            job["last_line"] = "Completed"
            await tb.maybe_auto_update_status_message(app, job, force=True)
            await tb.finish_job_card(app, job)
        except Exception as exc:
            if job.get("status") == "cancelled":
                return
            tb.logger.exception("Hentai playlist download failed")
            job["status"] = "failed"
            job["finished_at"] = tb.now_ts()
            job["last_line"] = str(exc)
            await tb.maybe_auto_update_status_message(app, job, force=True)
            await tb.finish_job_card(app, job)

    tb.run_in_background(tb.run_with_download_slot(app, job, run_playlist), name=f"batch-{job_id}")
    return job


async def start_pornhub_model_download(
    app: Application,
    chat_id: int,
    playlist: PornHubModelPlaylist,
    user_id: int = None,
):

    async with tb.jobs_lock:
        tb.job_counter += 1
        job_id = tb.job_counter

    video_count = len(playlist.urls)
    batch_mode = tb.normalize_batch_download_mode(
        tb.get_user_settings(user_id or 0).get("batch_download_mode")
    )
    job = {
        "id": job_id,
        "name": playlist.slug,
        "url": playlist.source_url,
        "chat_id": chat_id,
        "user_id": user_id or 0,
        "provider": "pornhub-model",
        "source_type": "adult_video_playlist",
        "platform": "PornHub",
        "pid": None,
        "process": None,
        "status": "starting",
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "upload_speed": 0,
        "eta": "Unknown",
        "started_at": tb.now_ts(),
        "finished_at": None,
        "last_line": "Preparing model videos...",
        "video_count": video_count,
        "completed_items": 0,
        "uploaded_items": 0,
        "deleted_items": 0,
        "batch_download_mode": batch_mode.value,
        "folder": str(tb.ADULT_VIDEO_DIR / "PornHub"),
    }
    tb.download_jobs[job_id] = job

    async def run_model_playlist():
        try:
            job["status"] = "downloading"
            await tb.maybe_auto_update_status_message(app, job, force=True)
            await tb.run_video_batch(
                app,
                chat_id,
                playlist.urls,
                job,
                item_label="Video",
                user_id=user_id,
            )
            if job.get("status") == "cancelled":
                return

            job["status"] = "completed"
            job["progress"] = 100.0
            job["finished_at"] = tb.now_ts()
            job["last_line"] = "Completed"
            await tb.maybe_auto_update_status_message(app, job, force=True)
            await tb.finish_job_card(app, job)
        except Exception as exc:
            if job.get("status") == "cancelled":
                return
            tb.logger.exception("PornHub model download failed")
            job["status"] = "failed"
            job["finished_at"] = tb.now_ts()
            job["last_line"] = str(exc)
            await tb.maybe_auto_update_status_message(app, job, force=True)
            await tb.finish_job_card(app, job)

    tb.run_in_background(
        tb.run_with_download_slot(app, job, run_model_playlist), name=f"batch-{job_id}"
    )
    return job


@tb.auto_answer
async def handle_link_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons on link prompts: lp:<request id>:<action>."""
    query = update.callback_query
    user_id = query.from_user.id
    _, rid, action = (query.data.split(":", 2) + ["", ""])[:3]
    request = tb.link_requests.pop(rid, None)
    if request is None:
        await tb.answer_once(
            query,
            "This prompt has expired or was already used. Send the link again.",
            show_alert=True,
        )
        return
    if request["kind"] == "page" and (action in ("others", "back") or action.startswith("pick")):
        # Moving around the page's videos: the prompt stays open.
        tb.link_requests[rid] = request
        await tb.answer_once(query)
        if action.startswith("pick") and action[4:].isdigit():
            request["index"] = min(int(action[4:]), len(request.get("candidates", [])) - 1)
        screen = (
            tb.page_others_screen(rid, request)
            if action == "others"
            else tb.page_picker_screen(rid, request)
        )
        await tb.safe_edit_message(query.message, *screen, parse_mode=ParseMode.HTML)
        return
    try:
        await tb._run_link_request(query, context, request, action, user_id)
    except Exception:
        # Nothing started: keep the prompt usable so another tap can retry.
        tb.link_requests.setdefault(rid, request)
        raise


async def _run_link_request(query, context, request: dict, action: str, user_id: int):
    await tb.answer_once(query)
    app = context.application
    chat_id = request["chat_id"]
    kind = request["kind"]

    if action == "cancel":
        await tb.safe_edit_message(query.message, f"{ICON_STOP} Cancelled. Nothing was downloaded.")
        return

    if kind == "video":
        audio_only = action == "mp3"
        max_height = int(action[1:]) if action.startswith("h") and action[1:].isdigit() else None
        job = await tb.start_ytdlp_download(
            app,
            chat_id,
            request["url"],
            audio_only=audio_only,
            user_id=user_id,
            run_in_background=True,
            max_height=max_height,
            gif=action == "gif",
        )
    elif kind == "page":
        if not request.get("candidates"):
            return  # still looking; the picker isn't up yet
        candidate = request["candidates"][request.get("index", 0)]
        audio_only = action == "mp3"
        max_height = int(action[1:]) if action.startswith("h") and action[1:].isdigit() else None
        job = await tb.start_ytdlp_download(
            app,
            chat_id,
            request["url"],
            audio_only=audio_only,
            user_id=user_id,
            run_in_background=True,
            max_height=max_height,
            gif=action == "gif",
            page={"title": request.get("title"), **candidate},
        )
    elif kind == "manga":
        job = await tb.start_manga_download(app, chat_id, request["url"], user_id)
    elif kind == "spotify":
        job = await tb.start_spotify_download(app, chat_id, request["url"], user_id)
    elif kind == "hentai":
        job = await tb.start_hentai_playlist_download(
            app, chat_id, request["playlist"], user_id=user_id
        )
    elif kind == "pornhub":
        job = await tb.start_pornhub_model_download(
            app, chat_id, request["playlist"], user_id=user_id
        )
    elif kind == "aria2":
        job = await tb.start_aria2_download(app, chat_id, request["source"], user_id)
    elif kind == "gallery":
        job = await tb.start_gallery_download(
            app, chat_id, request["url"], request["category"], user_id
        )
    else:
        return
    await tb.attach_job_card(app, job, message=query.message)


@tb.auto_answer
async def handle_stale_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons from prompts sent by an older version of the bot."""
    await tb.answer_once(
        update.callback_query,
        "This prompt is from an older version of the bot. Send the link again.",
        show_alert=True,
    )


async def _start_download_from_source(
    app: Application,
    chat_id: int,
    source: str,
    user_id: int = None,
):
    """Mini-app download entrypoint that chooses the right backend for pasted links."""
    source = source.strip()
    http_url = tb.extract_http_url(source)
    if tb.is_pornhub_model_url(http_url):
        playlist = await tb.resolve_pornhub_model_playlist(
            http_url,
            cookies_file=tb.cookies_file(),
            proxy=tb.YTDLP_PROXY or None,
        )
        if not playlist.urls:
            raise RuntimeError("No public videos found on this PornHub model page.")
        return await tb.start_pornhub_model_download(app, chat_id, playlist, user_id=user_id)
    if tb.is_hentai_playlist_url(http_url):
        playlist = await tb.resolve_hentai_playlist(http_url)
        if not playlist.urls:
            raise RuntimeError("No episode links found on this playlist page.")
        return await tb.start_hentai_playlist_download(app, chat_id, playlist, user_id=user_id)
    if tb.is_video_url(http_url):
        return await tb.start_ytdlp_download(
            app,
            chat_id,
            http_url,
            audio_only=False,
            user_id=user_id,
            run_in_background=True,
        )
    return await tb.start_aria2_download(app, chat_id, source, user_id)


async def start_download_from_source(
    app: Application,
    chat_id: int,
    source: str,
    user_id: int = None,
):
    """Mini App entry point: start the right backend and post a job card in the chat."""
    job = await tb._start_download_from_source(app, chat_id, source, user_id)
    if isinstance(job, dict) and job.get("id"):
        await tb.attach_job_card(app, job)
    return job
