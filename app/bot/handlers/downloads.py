"""Manga, gallery, Spotify and yt-dlp downloads."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

import yt_dlp
from pyrogram import StopTransmission
from telegram.ext import Application

from app.bot import telegram_bot as tb
from app.downloaders.base import DownloadRequest
from app.downloaders.spotify import SpotifyDownloader
from app.services import upload_filter
from app.services.batch_download import BatchDownloadMode, BatchProgress
from app.services.cobalt import CobaltError


async def start_manga_download(app: Application, chat_id: int, url: str, user_id: int = None):

    async with tb.jobs_lock:
        tb.job_counter += 1
        job_id = tb.job_counter

    job = {
        "id": job_id,
        "provider": "manga",
        "name": "Manga gallery",
        "url": url,
        "chat_id": chat_id,
        "user_id": user_id or 0,
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
        "last_line": "Fetching gallery images...",
        "image_count": 0,
        "folder": str(tb.MANGA_DIR),
        "pdf_path": "",
    }
    tb.download_jobs[job_id] = job

    async def run_job():
        try:
            result = await tb.download_manga_gallery(url, tb.MANGA_DIR)
            if job.get("status") == "cancelled":
                return
            job["status"] = "processing"
            job["name"] = result.title
            job["progress"] = 90.0
            job["image_count"] = len(result.images)
            job["folder"] = str(result.folder)
            job["last_line"] = "Images downloaded"
            settings = tb.get_user_settings(user_id or 0)
            if settings.get("manga_auto_convert_pdf"):
                job["last_line"] = "Converting images to PDF..."
                pdf_path = await tb.convert_manga_folder_to_pdf_job(result.folder, user_id or 0)
                job["pdf_path"] = str(pdf_path)
            job["status"] = "completed"
            job["progress"] = 100.0
            job["finished_at"] = tb.now_ts()
            job["last_line"] = "Completed"
            await tb.maybe_auto_update_status_message(app, job, force=True)
            await tb.finish_job_card(app, job)
        except Exception as e:
            tb.logger.exception("Manga download failed")
            job["status"] = "failed"
            job["finished_at"] = tb.now_ts()
            job["last_line"] = str(e)
            await tb.maybe_auto_update_status_message(app, job, force=True)
            await tb.finish_job_card(app, job)

    job["task"] = tb.run_in_background(
        tb.run_with_download_slot(app, job, run_job), name=f"manga-{job_id}"
    )
    return job


async def start_gallery_download(
    app: Application, chat_id: int, url: str, category: str, user_id: int = None
):
    """Download an image gallery/post with gallery-dl into Download/Gallery/<site>/."""

    async with tb.jobs_lock:
        tb.job_counter += 1
        job_id = tb.job_counter

    job = {
        "id": job_id,
        "provider": "gallery-dl",
        "category": category,
        "name": f"{tb.site_label(category)} · {tb.shorten(url.split('://', 1)[-1], 60)}",
        "url": url,
        "chat_id": chat_id,
        "user_id": user_id or 0,
        "pid": None,
        "process": None,
        "status": "starting",
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "eta": "Unknown",
        "started_at": tb.now_ts(),
        "finished_at": None,
        "last_line": "Looking for files…",
        "outputs": [],
    }
    tb.download_jobs[job_id] = job

    def set_process(process):
        job["process"] = process
        job["pid"] = process.pid

    def on_file(path: Path):
        job["status"] = "downloading"
        job["outputs"].append(str(path))
        with contextlib.suppress(OSError):
            job["completed_length"] += path.stat().st_size
        job["last_line"] = f"{len(job['outputs'])} file(s) so far · {path.name}"

    async def run_job():
        refresher = asyncio.create_task(tb._refresh_while_active(app, job))
        try:
            files = await tb.download_gallery(
                url,
                tb.GALLERY_DIR,
                on_file=on_file,
                set_process=set_process,
                cookies_file=tb.cookies_file(),
                proxy=tb.YTDLP_PROXY or None,
            )
            if job.get("status") == "cancelled":
                return
            job["outputs"] = [str(f) for f in files]
            job["status"] = "completed"
            job["progress"] = 100.0
            job["finished_at"] = tb.now_ts()
            job["last_line"] = f"{len(files)} file(s)"
            await tb.finish_job_card(app, job)
        except Exception as exc:
            if job.get("status") == "cancelled":
                return
            tb.logger.warning("gallery-dl download failed: %s", exc)
            reason, needs_cookies = tb.explain_gallery_error(exc, job.get("category"))
            if tb.cobalt_client.enabled and not job["outputs"]:
                try:
                    title, path = await tb.download_with_cobalt(
                        job,
                        url,
                        tb.GALLERY_DIR / (job.get("category") or "cobalt"),
                        reason,
                        lambda: job.get("status") == "cancelled",
                    )
                except CobaltError:
                    return  # cancelled
                except tb.DownloadFailed as failed:
                    reason = failed.reason
                else:
                    if path:
                        job["outputs"] = [path]
                    job.update(
                        name=title,
                        status="completed",
                        progress=100.0,
                        finished_at=tb.now_ts(),
                        last_line="",
                    )
                    await tb.finish_job_card(app, job)
                    return
            job["status"] = "failed"
            job["finished_at"] = tb.now_ts()
            job["last_line"] = reason
            job["needs_cookies"] = needs_cookies or tb._COOKIE_HINT in reason
            await tb.finish_job_card(app, job)
        finally:
            job["process"] = None
            refresher.cancel()

    job["task"] = tb.run_in_background(
        tb.run_with_download_slot(app, job, run_job), name=f"gallery-{job_id}"
    )
    return job


async def _refresh_while_active(app: Application, job: dict):
    """Refresh a job's card while it runs, for providers without their own progress loop."""
    while job.get("status") in tb.JOB_ACTIVE_STATES:
        await tb.maybe_auto_update_status_message(app, job)
        await asyncio.sleep(2)


async def start_spotify_download(app: Application, chat_id: int, url: str, user_id: int = None):

    async with tb.jobs_lock:
        tb.job_counter += 1
        job_id = tb.job_counter

    job = {
        "id": job_id,
        "provider": "spotify",
        "name": "Spotify download",
        "url": url,
        "chat_id": chat_id,
        "user_id": user_id or 0,
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
        "last_line": "Waiting for spotDL...",
        "artifact_count": 0,
    }
    tb.download_jobs[job_id] = job

    def set_process(process):
        job["process"] = process
        job["pid"] = getattr(process, "pid", None)

    def update_progress(line: str, percent: float | None):
        job["status"] = "downloading"
        job["last_line"] = tb.shorten(line, 220)
        if percent is not None:
            job["progress"] = percent

    async def run_job():
        provider = SpotifyDownloader(
            spotdl_bin=tb.SPOTDL_BIN,
            ffmpeg_bin=tb.FFMPEG_BIN,
            cookie_file=tb.cookies_file(),
            proxy=tb.YTDLP_PROXY or None,
        )
        try:
            result = await provider.download(
                DownloadRequest(
                    url=url,
                    destination=tb.SPOTIFY_DIR,
                    options={
                        "process_callback": set_process,
                        "progress_callback": update_progress,
                    },
                )
            )
            if job.get("status") == "cancelled":
                return
            total_size = sum(
                artifact.size_bytes or 0
                for artifact in result.artifacts
                if artifact.media_type == "audio"
            )
            job["status"] = "completed"
            job["name"] = result.title
            job["progress"] = 100.0
            job["completed_length"] = total_size
            job["total_length"] = total_size
            job["artifact_count"] = len(result.artifacts)
            if not result.artifacts:
                job["note"] = "already downloaded"
            job["outputs"] = [
                str(artifact.path)
                for artifact in result.artifacts
                if artifact.media_type == "audio"
            ]
            job["finished_at"] = tb.now_ts()
            job["last_line"] = "Completed"
            await tb.maybe_auto_update_status_message(app, job, force=True)
            await tb.finish_job_card(app, job)
        except Exception as e:
            if job.get("status") == "cancelled":
                return
            reason, needs_cookies = tb.explain_spotdl_error(e)
            tb.logger.warning("spotDL download failed: %s", e)
            job["status"] = "failed"
            job["finished_at"] = tb.now_ts()
            job["last_line"] = reason
            job["needs_cookies"] = needs_cookies
            await tb.maybe_auto_update_status_message(app, job, force=True)
            await tb.finish_job_card(app, job)
        finally:
            job["process"] = None

    job["task"] = tb.run_in_background(
        tb.run_with_download_slot(app, job, run_job), name=f"spotify-{job_id}"
    )
    return job


async def start_ytdlp_download(
    app: Application,
    chat_id: int,
    url: str,
    audio_only: bool = False,
    user_id: int = None,
    run_in_background: bool = False,
    notify: bool = True,
    parent_job: dict | None = None,
    max_height: int | None = None,
    gif: bool = False,
):
    """Download one video with yt-dlp (``max_height`` caps the resolution).

    ``gif`` makes a silent clip that is sent to the chat as a GIF. When yt-dlp
    fails and a cobalt instance is configured, cobalt gets a try.

    ``parent_job`` is the batch (playlist/model page) this item belongs to;
    cancelling the batch aborts the item that is downloading.
    """

    async with tb.jobs_lock:
        tb.job_counter += 1
        job_id = tb.job_counter

    is_hentai = tb.is_hentai_video_url(url)
    is_adult = tb.is_adult_video_url(url)
    platform = tb.video_platform_label(url)
    if gif:
        audio_only, max_height = False, min(max_height or tb.GIF_MAX_HEIGHT, tb.GIF_MAX_HEIGHT)
    if is_hentai:
        output_dir = tb.HENTAI_VIDEO_DIR / tb.video_platform_slug(url)
    elif is_adult:
        output_dir = tb.ADULT_VIDEO_DIR / tb.video_platform_slug(url)
    else:
        output_dir = tb.DOWNLOAD_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    job = {
        "id": job_id,
        "name": "Fetching video info...",
        "url": url,
        "chat_id": chat_id,
        "user_id": user_id or 0,
        "provider": "yt-dlp",
        "source_type": "hentai_video" if is_hentai else "adult_video" if is_adult else "video",
        "platform": platform,
        "pid": None,
        "process": None,
        "status": "starting",
        "status_visible": notify,
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "upload_speed": 0,
        "eta": "Unknown",
        "started_at": tb.now_ts(),
        "finished_at": None,
        "last_line": "",
        "folder": str(output_dir),
        "filepath": "",
        "audio_only": audio_only,
        "max_height": None if audio_only else max_height,
        "gif": gif,
    }

    tb.download_jobs[job_id] = job

    loop = asyncio.get_running_loop()

    def is_cancelled() -> bool:
        return job.get("status") == "cancelled" or bool(
            parent_job and parent_job.get("status") == "cancelled"
        )

    def progress_hook(d):
        # Outside the try below: that block logs and swallows exceptions, and
        # this one has to reach yt-dlp to abort the download.
        if is_cancelled():
            raise yt_dlp.utils.DownloadCancelled("Cancelled by user")
        if d.get("tmpfilename"):
            job["tmpfile"] = d["tmpfilename"]
        title = (d.get("info_dict") or {}).get("title")
        if title and job.get("name") == "Fetching video info...":
            job["name"] = title
        try:
            status = d.get("status")

            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0

                downloaded = d.get("downloaded_bytes", 0)
                speed = d.get("speed") or 0
                eta = d.get("eta")

                pct = (downloaded / total * 100) if total else 0

                job["status"] = "downloading"
                job["progress"] = pct
                job["completed_length"] = downloaded
                job["total_length"] = total
                job["download_speed"] = speed
                job["eta"] = tb.format_eta(eta)
                refusal = tb.ytdlp_disk_check(job, total, downloaded, loop)
                if refusal:
                    job["disk_refused"] = refusal

            elif status == "finished":
                job["status"] = "processing"
                job["progress"] = 100.0

        except Exception as e:
            tb.logger.exception(f"yt-dlp progress hook error: {e}")
        if job.get("disk_refused"):
            # Outside the try for the same reason as the cancel check above.
            raise tb.DownloadFailed(job["disk_refused"])

    async def refresh_ytdlp_status():
        if not notify:
            return
        while job["status"] in tb.JOB_ACTIVE_STATES:
            await tb.maybe_auto_update_status_message(app, job)
            await asyncio.sleep(1)

    def run_download():
        if tb.requires_deno_runtime(url) and not tb.DENO_VERSION:
            raise RuntimeError(
                "Hanime requires Deno. Install Deno, ensure `deno --version` works for "
                "the bot service user, or set DENO_BIN=/absolute/path/to/deno in .env."
            )
        resolved_video = tb.resolve_adult_video_url(url)
        download_url = resolved_video.url
        existing_files = {path.resolve() for path in output_dir.iterdir() if path.is_file()}
        output_template = str(output_dir / "%(title).200B [%(id)s].%(ext)s")
        if resolved_video.referer:
            output_template = tb.resolved_video_output_template(output_dir, url)

        ydl_opts = tb.ytdlp_common_options(url, resolved_video)
        ydl_opts.update(
            {
                "outtmpl": output_template,
                "progress_hooks": [progress_hook],
                "concurrent_fragment_downloads": 4,
                "fragment_retries": 10,
                "file_access_retries": 3,
                "continuedl": True,
                "part": True,
                "windowsfilenames": False,
            }
        )

        if audio_only:
            ydl_opts.update(
                {
                    "format": "bestaudio/best",
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }
                    ],
                }
            )
        else:
            ydl_opts.update(
                {
                    "format": tb.video_format_selector(max_height),
                    "merge_output_format": "mp4",
                }
            )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(download_url, download=True)

            if info is None:
                raise RuntimeError("Failed to extract video info")

            title = info.get("title") or "Unknown Video"

            filepath_candidates = []
            requested = info.get("requested_downloads") or []
            filepath_candidates.extend(item.get("filepath") for item in requested)
            filepath_candidates.extend(
                [
                    info.get("filepath"),
                    info.get("_filename"),
                    ydl.prepare_filename(info),
                ]
            )

            if audio_only:
                filepath_candidates.extend(
                    f"{os.path.splitext(candidate)[0]}.mp3"
                    for candidate in list(filepath_candidates)
                    if candidate
                )
            else:
                filepath_candidates.extend(
                    f"{os.path.splitext(candidate)[0]}.mp4"
                    for candidate in list(filepath_candidates)
                    if candidate
                )

            filepath = next(
                (
                    str(Path(candidate).resolve())
                    for candidate in filepath_candidates
                    if candidate and Path(candidate).is_file()
                ),
                None,
            )
            if not filepath:
                new_files = [
                    path
                    for path in output_dir.iterdir()
                    if path.is_file()
                    and path.resolve() not in existing_files
                    and path.suffix not in {".part", ".ytdl"}
                ]
                if new_files:
                    filepath = str(max(new_files, key=lambda path: path.stat().st_mtime).resolve())

            return title, filepath

    def remove_partial_files():
        tmp = job.get("tmpfile")
        if not tmp:
            return
        tmp_path = Path(tmp)
        try:
            for leftover in tmp_path.parent.iterdir():
                # Prefix match, not glob(): titles often contain "[...]".
                if leftover.is_file() and leftover.name.startswith(tmp_path.name):
                    leftover.unlink(missing_ok=True)
        except OSError as exc:
            tb.logger.warning("Could not remove partial download %s: %s", tmp, exc)

    async def finish_cancelled():
        job["status"] = "cancelled"
        job["finished_at"] = job.get("finished_at") or tb.now_ts()
        job["last_line"] = "Cancelled by user"
        await asyncio.to_thread(remove_partial_files)
        if notify:
            await tb.finish_job_card(app, job)

    async def finish_download():
        try:
            # A playlist item runs under its batch's slot; waiting for one of
            # its own would deadlock at a limit of 1.
            if parent_job is None and not await tb.wait_for_download_slot(app, job, is_cancelled):
                if job.get("status") != "failed":  # failed: refused for disk space, card is final
                    await finish_cancelled()
                return
            await download()
        finally:
            tb.download_slots.release(job.get("user_id") or 0, job_id)

    async def download():
        refresh_task = asyncio.create_task(refresh_ytdlp_status())
        try:
            try:
                title, filepath = await loop.run_in_executor(None, run_download)
            except Exception as ytdlp_error:
                if (
                    is_cancelled()
                    or job.get("disk_refused")
                    or not tb.cobalt_client.enabled
                    or is_hentai
                    or is_adult
                ):
                    raise
                title, filepath = await tb.download_with_cobalt(
                    job, url, output_dir, tb.explain_ytdlp_error(ytdlp_error)[0], is_cancelled
                )
            if is_cancelled():
                await finish_cancelled()
                return

            job["status"] = "completed"
            job["name"] = title
            job["progress"] = 100.0
            if filepath and os.path.exists(filepath):  # noqa: ASYNC240
                size = os.path.getsize(filepath)  # noqa: ASYNC240
                job["completed_length"] = size
                job["total_length"] = size
                job["filepath"] = str(Path(filepath).resolve())  # noqa: ASYNC240
            job["download_speed"] = 0
            job["finished_at"] = tb.now_ts()
            if notify:
                await tb.maybe_auto_update_status_message(app, job, force=True)

            if notify:
                await tb.finish_job_card(app, job)

        except Exception as e:
            if is_cancelled():
                await finish_cancelled()
                return
            if job.get("disk_refused"):
                reason, fatal = job["disk_refused"], True
                await asyncio.to_thread(remove_partial_files)
            elif isinstance(e, tb.DownloadFailed):
                reason, fatal = e.reason, True
            else:
                reason, fatal = tb.explain_ytdlp_error(e)
            if fatal:
                tb.logger.warning("yt-dlp download failed: %s", e)
            else:
                tb.logger.exception("yt-dlp download failed")

            job["status"] = "failed"
            job["finished_at"] = tb.now_ts()
            job["last_line"] = reason
            job["needs_cookies"] = tb._COOKIE_HINT in reason
            if notify:
                await tb.maybe_auto_update_status_message(app, job, force=True)

            if notify:
                await tb.finish_job_card(app, job)
        finally:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task

    if run_in_background:
        asyncio.create_task(finish_download())
    else:
        await finish_download()

    return job


async def upload_and_delete_batch_artifact(
    app: Application,
    job: dict,
    filepath: str,
    item_index: int,
    total_items: int,
) -> bool:
    """Upload one completed batch artifact and remove it only after success."""
    full = Path(filepath).resolve()  # noqa: ASYNC240
    try:
        rel_path = tb.file_rel_path(full)
    except ValueError as exc:
        raise RuntimeError(
            f"Downloaded file is outside the managed Download folder: {full}"
        ) from exc
    if not full.is_file():
        raise FileNotFoundError(str(full))
    refusal = await tb.upload_refusal(full)
    if refusal:
        # Kept on disk, not deleted: it can be uploaded from Files once complete.
        tb.logger.info("Upload of %s refused: %s", rel_path, refusal)
        job["last_line"] = f"{full.name}: {refusal}"
        await tb.maybe_auto_update_status_message(app, job, force=True)
        return False

    target = tb.upload_target(tb.get_user_settings(job.get("user_id") or 0))
    # Held through the preview so this video and its preview stay adjacent.
    async with tb._send_chain(job["chat_id"], target):
        cap = await tb.upload_part_cap(await tb.get_pyrogram_client())
        chunks, as_document = await asyncio.to_thread(tb.prepare_upload_parts, rel_path, cap)
        try:
            for chunk_path, chunk_index, chunk_total in chunks:
                if job.get("status") == "cancelled":
                    return False
                chunk = Path(chunk_path).resolve()  # noqa: ASYNC240
                chunk_rel_path = tb.file_rel_path(chunk)

                async def progress(current, total, *, part=chunk_index, parts=chunk_total):
                    if job.get("status") == "cancelled":
                        raise StopTransmission()
                    percent = (current / total * 100) if total else 0.0
                    part_text = f", part {part}/{parts}" if parts > 1 else ""
                    job["status"] = "uploading"
                    job["last_line"] = (
                        f"Uploading {item_index}/{total_items}{part_text}: "
                        f"{full.name} ({percent:.1f}%)"
                    )
                    job["upload_length"] = int(current or 0)
                    await tb.maybe_auto_update_status_message(app, job)

                job["status"] = "uploading"
                job["last_line"] = f"Uploading {item_index}/{total_items}: {full.name}"
                await tb.maybe_auto_update_status_message(app, job, force=True)
                try:
                    await tb.pyrogram_send_file(
                        chunk_rel_path,
                        progress_callback=progress,
                        chat_id=target,
                        force_document=as_document,
                        max_size=cap,
                    )
                except StopTransmission:
                    return False
        finally:
            tb.cleanup_file_chunks(chunks)

        if job.get("status") == "cancelled":
            return False

        if tb.wants_upload_previews(job.get("user_id"), full):
            await tb.send_upload_previews(app, job["chat_id"], target, rel_path, delete_after=True)
        else:
            full.unlink()
            await asyncio.to_thread(tb.prune_empty_folders, full)
    job["uploaded_items"] = int(job.get("uploaded_items", 0) or 0) + 1
    job["deleted_items"] = int(job.get("deleted_items", 0) or 0) + 1
    job["upload_length"] = 0
    return True


async def run_video_batch(
    app: Application,
    chat_id: int,
    urls: list[str] | tuple[str, ...],
    job: dict,
    *,
    item_label: str,
    user_id: int | None,
) -> None:
    """Run provider batch items sequentially using the job's snapshotted mode."""
    mode = tb.normalize_batch_download_mode(job.get("batch_download_mode"))
    preset = tb.get_user_settings(user_id or 0).get("auto_upload_types")

    async def process_item(item_url: str, index: int, total_items: int) -> dict:
        child = await tb.start_ytdlp_download(
            app,
            chat_id,
            item_url,
            audio_only=False,
            user_id=user_id,
            notify=False,
            parent_job=job,
        )
        if child.get("status") != "completed":
            reason = child.get("last_line") or child.get("status") or "Unknown error"
            raise RuntimeError(f"{item_label} {index} failed: {reason}")
        return child

    async def after_item(child: dict, index: int, total_items: int) -> None:
        filepath = str(child.get("filepath") or "")
        if mode is BatchDownloadMode.UPLOAD_AND_DELETE:
            if not filepath:
                raise RuntimeError(f"{item_label} {index} completed without an output file path.")
            if not upload_filter.matches(preset, filepath):
                full = Path(filepath).resolve()  # noqa: ASYNC240
                full.unlink(missing_ok=True)  # noqa: ASYNC240
                await asyncio.to_thread(tb.prune_empty_folders, full)
                job["filtered_items"] = int(job.get("filtered_items", 0) or 0) + 1
                job["last_line"] = (
                    f"Deleted {item_label.lower()} {index}/{total_items}: "
                    f"not {upload_filter.label(preset)}"
                )
                tb.logger.info(
                    "Auto-upload filter %s deleted %s from job #%s",
                    upload_filter.normalize(preset),
                    full.name,
                    job.get("id"),
                )
                return
            deleted = await tb.upload_and_delete_batch_artifact(
                app, job, filepath, index, total_items
            )
            if deleted:
                job["last_line"] = (
                    f"Uploaded and deleted {item_label.lower()} {index}/{total_items}"
                )
        else:
            job["last_line"] = f"Downloaded {item_label.lower()} {index}/{total_items}"
            if filepath:
                job.setdefault("outputs", []).append(filepath)

    async def on_progress(progress: BatchProgress) -> None:
        job["current_item"] = progress.current
        job["completed_items"] = progress.completed
        job["progress"] = (progress.completed / progress.total * 100) if progress.total else 100.0
        if progress.phase == "processing":
            job["status"] = "downloading"
            job["last_line"] = (
                f"Downloading {item_label.lower()} {progress.current}/{progress.total}"
            )
        await tb.maybe_auto_update_status_message(app, job, force=True)

    await tb.run_sequential_batch(
        urls,
        process_item,
        after_item=after_item,
        on_progress=on_progress,
        is_cancelled=lambda: job.get("status") == "cancelled",
    )
    filtered = int(job.get("filtered_items", 0) or 0)
    if filtered and not job.get("uploaded_items"):
        job["auto_upload_note"] = upload_filter.filtered_out_note(preset, filtered)
    elif filtered:
        job["auto_upload_note"] = (
            f"🧹 {filtered} item(s) deleted by the auto-upload filter ({upload_filter.label(preset)})"
        )
