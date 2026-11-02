"""aria2 torrent/HTTP jobs: monitoring, control and torrent file selection."""

from __future__ import annotations

import asyncio
import contextlib
import math
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote_plus

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, ContextTypes

from app.bot import telegram_bot as tb
from app.bot.icons import ICON_FAIL, ICON_MAGNET
from app.infrastructure.aria2_rpc import Aria2ConnectionError, Aria2RpcError


def _parse_int_field(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def extract_info_hash(text: str) -> str:
    """Return a normalized torrent info hash from a magnet or aria2 message."""
    decoded = unquote_plus(text or "")
    match = re.search(r"(?:btih:|InfoHash\s+)([A-Za-z0-9]+)", decoded, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _same_info_hash(left: str | None, right: str | None) -> bool:
    return bool(left and right and str(left).lower() == str(right).lower())


async def find_aria2_status_by_info_hash(info_hash: str) -> dict | None:
    if not info_hash:
        return None

    batches = [
        await tb.aria2_client.tell_active(),
        await tb.aria2_client.tell_waiting(0, 100),
        await tb.aria2_client.tell_stopped(0, 100),
    ]
    for status in [item for batch in batches for item in batch]:
        if tb._same_info_hash(status.get("infoHash"), info_hash):
            return status
    return None


def _format_eta(total_length: int, completed_length: int, download_speed: int) -> str:
    if not total_length or not download_speed or completed_length >= total_length:
        return "Unknown"
    seconds = max(0, math.ceil((total_length - completed_length) / download_speed))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _extract_rpc_name(status: dict, fallback: str) -> str:
    bt_name = (status.get("bittorrent") or {}).get("info", {}).get("name")
    if bt_name:
        return tb.clean_download_name(bt_name)
    for file_info in status.get("files") or []:
        path = file_info.get("path")
        if path:
            return tb.clean_download_name(Path(path).name)
    return tb.clean_download_name(fallback)


def _apply_aria2_status(job: dict, status: dict):
    total_length = tb._parse_int_field(status.get("totalLength"))
    completed_length = tb._parse_int_field(status.get("completedLength"))
    download_speed = tb._parse_int_field(status.get("downloadSpeed"))
    upload_length = tb._parse_int_field(status.get("uploadLength"))
    upload_speed = tb._parse_int_field(status.get("uploadSpeed"))
    connections = tb._parse_int_field(status.get("connections"))
    num_seeders = tb._parse_int_field(status.get("numSeeders"))
    rpc_status = status.get("status", "unknown")

    job["aria2_status"] = rpc_status
    job["aria2_gid"] = status.get("gid", job.get("gid"))
    job["followed_by"] = status.get("followedBy") or []
    job["following"] = status.get("following")
    job["info_hash"] = status.get("infoHash") or job.get("info_hash")
    job["connections"] = connections
    job["num_seeders"] = num_seeders
    job["total_length"] = total_length
    job["completed_length"] = completed_length
    job["download_speed"] = download_speed
    job["upload_length"] = upload_length
    job["upload_speed"] = upload_speed
    job["progress"] = (completed_length / total_length * 100) if total_length else 0.0
    job["eta"] = tb._format_eta(total_length, completed_length, download_speed)
    job["name"] = tb._extract_rpc_name(status, job["name"])
    selected = [
        f.get("path")
        for f in status.get("files") or []
        if f.get("path") and str(f.get("selected", "true")) == "true"
    ]
    if selected:
        job["files"] = selected

    if rpc_status == "active":
        job["status"] = (
            "metadata" if not total_length and job.get("source_type") == "magnet" else "downloading"
        )
    elif rpc_status == "waiting":
        job["status"] = "queued"
    elif rpc_status == "paused":
        job["status"] = "paused"
    elif rpc_status == "complete":
        if status.get("followedBy"):
            job["status"] = "metadata"
        else:
            job["status"] = "completed"
            job["progress"] = 100.0
    elif rpc_status == "error":
        job["status"] = "failed"
        message = status.get("errorMessage") or status.get("errorCode") or "Unknown error"
        job["last_line"] = str(message)
    elif rpc_status == "removed":
        job["status"] = "cancelled"

    # A torrent that is still at zero peers and zero bytes after a few minutes
    # is almost always blocked from reaching trackers/peers; say so on the card
    # instead of "Fetching torrent metadata…" forever.
    job["unreachable"] = (
        rpc_status == "active"
        and job.get("source_type") in ("magnet", "torrent")
        and connections == 0
        and completed_length == 0
        and tb.now_ts() - (job.get("started_at") or tb.now_ts()) >= tb.TORRENT_UNREACHABLE_AFTER
    )


def _split_torrent_source(source: str) -> tuple[str, str | None]:
    if " --select-file=" in source:
        torrent_path, selected = source.split(" --select-file=", 1)
        return torrent_path.strip(), selected.strip()
    return source.strip(), None


def _is_local_torrent_file(source: str) -> bool:
    lowered = source.lower()
    if lowered.startswith(("magnet:", "http://", "https://")):
        return False

    try:
        source_path = Path(source)
        return source_path.suffix.lower() == ".torrent" and source_path.exists()
    except OSError:
        return False


def switch_to_followed_gid(job: dict, status: dict) -> bool:
    followed_by = status.get("followedBy") or []
    if not followed_by:
        return False

    next_gid = followed_by[0]
    if not next_gid or next_gid == job.get("gid"):
        return False

    previous_gid = job.get("gid")
    job["metadata_gid"] = previous_gid
    job.setdefault("gid_history", []).append(previous_gid)
    job["gid"] = next_gid
    job["aria2_gid"] = next_gid
    job["status"] = "queued"
    job["aria2_status"] = "waiting"
    job["last_line"] = "Torrent metadata resolved; following real download."
    return True


async def fetch_torrent_file(info_hash: str) -> Path | None:
    """Download a magnet's .torrent over HTTPS from itorrents.org.

    HTTPS still works on hosts whose firewalls block tracker and DHT ports,
    so this gets the metadata (and its tracker list) when the magnet can't.
    """
    if not re.fullmatch(r"[0-9a-fA-F]{40}", info_hash or ""):
        return None
    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}
        ) as client:
            response = await client.get(tb.ITORRENTS_URL.format(info_hash.upper()))
    except httpx.HTTPError as exc:
        tb.logger.warning("itorrents fetch failed for %s: %s", info_hash, exc)
        return None
    body = response.content
    if response.status_code != 200 or not body.startswith(b"d") or b"4:info" not in body:
        return None
    path = tb.DOWNLOAD_DIR / ".torrents" / f"{info_hash.lower()}.torrent"
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, body)
    return path


async def maybe_resolve_magnet_via_torrent_file(job: dict, status: dict) -> bool:
    """Swap a magnet stuck at 0 peers for its .torrent from itorrents.org, once."""
    if (
        job.get("source_type") != "magnet"
        or job.get("metadata_gid")
        or job.get("torrent_fallback_tried")
        or status.get("status") != "active"
        or tb._parse_int_field(status.get("connections"))
        or tb.now_ts() - (job.get("started_at") or tb.now_ts()) < tb.MAGNET_RESOLVE_AFTER
    ):
        return False
    job["torrent_fallback_tried"] = True
    torrent_path = await tb.fetch_torrent_file(tb.extract_info_hash(job.get("magnet") or ""))
    if not torrent_path:
        return False

    old_gid = job["gid"]
    with contextlib.suppress(Aria2RpcError):
        await tb.aria2_client.remove(old_gid, force=True)
    _, selected_files = tb._split_torrent_source(job.get("source") or "")
    spec = f"{torrent_path} --select-file={selected_files}" if selected_files else str(torrent_path)
    # aria2 keeps the info hash registered until the removal settles.
    for _ in range(20):
        gid, existing, _ = await tb.aria2_add_source(spec)
        if existing is None or gid != old_gid:
            break
        await asyncio.sleep(0.5)
    else:
        return False

    job["metadata_gid"] = old_gid
    job.setdefault("gid_history", []).append(gid)
    job["gid"] = gid
    job["aria2_gid"] = gid
    job["last_line"] = "Magnet found no peers; fetched its .torrent from itorrents.org."
    tb.logger.info("Job %s: magnet %s replaced by .torrent (gid %s)", job.get("id"), old_gid, gid)
    return True


async def monitor_aria2_job(app: Application, job_id: int):
    job = tb.download_jobs[job_id]
    user_id = job.get("user_id") or 0
    try:
        if job["status"] in tb.JOB_ACTIVE_STATES and not tb.download_slots.try_acquire(
            user_id, job_id, tb.limit_of(tb.get_user_settings(user_id))
        ):
            # Over the limit: hold it paused in aria2 until a slot frees up.
            job["slot_held"] = True
            try:
                await tb.aria2_client.pause(job["gid"], force=True)
            except Aria2RpcError as exc:
                tb.logger.debug("Could not hold job %s in the queue: %s", job_id, exc)
            # Disk space is checked once aria2 knows the size (aria2_disk_gate).
            if not await tb.wait_for_download_slot(app, job, check_disk=False):
                return  # cancelled while queued; cancel_job removed it from aria2
        job.pop("queue_position", None)
        # Held by the queue (also before a restart) and not paused by the user since.
        if job.pop("slot_held", False) and job["status"] != "paused":
            try:
                await tb.aria2_client.unpause(job["gid"])
            except Aria2RpcError as exc:
                tb.logger.debug("Could not start queued job %s: %s", job_id, exc)
        await tb._monitor_aria2_job(app, job)
    finally:
        tb.download_slots.release(user_id, job_id)


async def _monitor_aria2_job(app: Application, job: dict):
    job_id = job["id"]

    while job["status"] not in ("completed", "failed", "cancelled"):
        try:
            status = await tb.aria2_client.tell_status(job["gid"])
            tb._apply_aria2_status(job, status)
            if tb.switch_to_followed_gid(
                job, status
            ) or await tb.maybe_resolve_magnet_via_torrent_file(job, status):
                await tb.maybe_auto_update_status_message(app, job, force=True)
                await asyncio.sleep(1)
                continue
            if await tb.aria2_disk_gate(app, job, status):
                await asyncio.sleep(1)
                continue
            await tb.maybe_auto_update_status_message(app, job)
        except Aria2RpcError as exc:
            if not isinstance(exc, Aria2ConnectionError):
                if "not found" in str(exc).lower() and job["status"] == "cancelled":
                    return
                job["status"] = "failed"
                job["last_line"] = str(exc)
                break
            transient_error = exc
        except Exception as exc:
            transient_error = exc
        else:
            transient_error = None
        if transient_error is not None:
            # A dropped RPC connection, or aria2 restarting (it resumes its
            # session under the same GIDs), must not fail the job at once.
            # Retry for a while before giving up.
            exc = transient_error
            job["monitor_errors"] = job.get("monitor_errors", 0) + 1
            tb.logger.warning("aria2 monitor error for job %s: %s", job_id, exc)
            if job["monitor_errors"] >= 30:
                job["status"] = "failed"
                job["last_line"] = f"Lost contact with aria2: {exc}"
                break
            await asyncio.sleep(5)
            continue
        job["monitor_errors"] = 0

        if status.get("status") in tb.ARIA2_DONE_STATES:
            break

        await asyncio.sleep(2)

    if job["status"] == "cancelled":
        job["finished_at"] = tb.now_ts()
        await tb.finish_job_card(app, job)
        return

    if job["status"] == "completed":
        job["status"] = "completed"
        tb.logger.info(f"Download completed: {job['name']}")
        job["progress"] = 100.0
        job["finished_at"] = tb.now_ts()
        await tb.maybe_auto_update_status_message(app, job, force=True)

        await tb.finish_job_card(app, job)

    else:
        job["status"] = "failed"
        tb.logger.error(f"Download failed: {job['name']}")
        job["finished_at"] = tb.now_ts()
        await tb.maybe_auto_update_status_message(app, job, force=True)

        await tb.finish_job_card(app, job)


async def aria2_add_source(source_spec: str) -> tuple[str, dict | None, str]:
    """Add a magnet/URL/.torrent (optionally ``path --select-file=1,2``) to aria2.

    If aria2 already has that torrent, return its existing download instead:
    (gid, status of the existing download or None, info hash).
    """
    source, selected_files = tb._split_torrent_source(source_spec)
    info_hash = tb.extract_info_hash(source)
    options = {
        "dir": str(tb.DOWNLOAD_DIR),
        "continue": "true",
        # Re-adding a torrent whose file is already on disk (finished, or its
        # control file lost) hash-checks it instead of failing with errorCode 13.
        "check-integrity": "true",
        "follow-torrent": "true",
        "bt-save-metadata": "true",
        "bt-metadata-only": "false",
        "seed-time": "0",
    }
    if selected_files:
        options["select-file"] = selected_files
    if source.lower().startswith("magnet:") or tb._is_local_torrent_file(source):
        options["bt-tracker"] = ",".join(tb.DEFAULT_BT_TRACKERS)
    if source.lower().startswith("magnet:") and info_hash:
        # aria2 accepts a magnet it already has and only fails it later with
        # "InfoHash ... is already registered", so check for a live copy first.
        existing = await tb.find_aria2_status_by_info_hash(info_hash)
        if existing and existing.get("status") in ("active", "waiting", "paused"):
            return existing["gid"], existing, info_hash
    try:
        if tb._is_local_torrent_file(source):
            gid = await tb.aria2_client.add_torrent(Path(source), options)
        else:
            gid = await tb.aria2_client.add_uri(source, options)
        return gid, None, info_hash
    except Aria2RpcError as exc:
        duplicate_hash = tb.extract_info_hash(str(exc)) or info_hash
        if "already registered" not in str(exc).lower() or not duplicate_hash:
            raise
        existing = await tb.find_aria2_status_by_info_hash(duplicate_hash)
        if not existing:
            raise
        return existing["gid"], existing, duplicate_hash


async def start_aria2_download(app: Application, chat_id: int, magnet: str, user_id: int = None):

    source, selected_files = tb._split_torrent_source(magnet)
    is_torrent_file = tb._is_local_torrent_file(source)
    lowered_source = source.lower()
    is_http_uri = lowered_source.startswith(("http://", "https://"))
    source_type = (
        "torrent"
        if is_torrent_file
        else "magnet" if lowered_source.startswith("magnet:") else "http" if is_http_uri else "uri"
    )
    if source_type == "magnet":
        name = tb.extract_bt_name(source)
    elif source_type == "http":
        name = tb.extract_http_filename(source)
    elif is_torrent_file:
        name = tb.clean_download_name(Path(source).name)
    else:
        name = "Download"
    info_hash = tb.extract_info_hash(source)
    gid, initial_status, found_hash = await tb.aria2_add_source(magnet)
    reattached = initial_status is not None
    info_hash = found_hash or info_hash

    async with tb.jobs_lock:
        tb.job_counter += 1
        job_id = tb.job_counter

    job = {
        "id": job_id,
        "name": name,
        "source": magnet,
        "magnet": source,
        "gid": gid,
        "gid_history": [gid],
        "metadata_gid": None,
        "source_type": source_type,
        "chat_id": chat_id,
        "user_id": user_id or 0,
        "pid": tb.aria2_client.pid,
        "process": None,
        "status": "starting",
        "aria2_status": "starting",
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "upload_length": 0,
        "upload_speed": 0,
        "connections": 0,
        "num_seeders": 0,
        "info_hash": info_hash,
        "followed_by": [],
        "following": None,
        "eta": "Unknown",
        "started_at": tb.now_ts(),
        "finished_at": None,
        "last_line": "Reattached to existing aria2 download." if reattached else "",
    }
    if initial_status:
        tb._apply_aria2_status(job, initial_status)
        tb.switch_to_followed_gid(job, initial_status)

    tb.download_jobs[job_id] = job

    asyncio.create_task(tb.monitor_aria2_job(app, job_id))

    return job


async def cancel_job(job_id: int):
    job = tb.download_jobs.get(job_id)
    if not job:
        return False, f"Job #{job_id} not found."

    if job["status"] in ("completed", "failed", "cancelled"):
        return False, f"Job #{job_id} is already {job['status']}."

    process = job.get("process")
    if process is None and job.get("provider") in ("spotify", "gallery-dl"):
        # Still starting up: no subprocess yet, so stop the task that would start it.
        job["status"] = "cancelled"
        job["finished_at"] = tb.now_ts()
        job["last_line"] = "Cancelled by user"
        task = job.get("task")
        if task is not None and not task.done():
            task.cancel()
        return True, f"Cancelled job #{job_id}: {job['name']}"
    if process is not None and job.get("provider") in ("spotify", "gallery-dl"):
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            pass
        except Exception as exc:
            return False, f"Could not cancel job #{job_id}: {exc}"

        job["status"] = "cancelled"
        job["finished_at"] = tb.now_ts()
        job["last_line"] = "Cancelled by user"
        return True, f"Cancelled job #{job_id}: {job['name']}"

    if job.get("provider") in {"manga", "yt-dlp", "hentai-playlist", "pornhub-model"}:
        # yt-dlp items (including the one a batch is on) see this status in
        # their progress hook and abort; manga downloads are cancelled directly.
        job["status"] = "cancelled"
        job["finished_at"] = tb.now_ts()
        job["last_line"] = "Cancelled by user"
        task = job.get("task")
        if task is not None and not task.done():
            task.cancel()
        return True, f"Cancelled job #{job_id}: {job['name']}"

    errors = []
    for gid in dict.fromkeys([job.get("gid"), job.get("metadata_gid")]):
        if not gid:
            continue
        try:
            await tb.aria2_client.remove(gid, force=True)
        except Aria2RpcError as exc:
            if "not found" not in str(exc).lower():
                errors.append(str(exc))
    if errors:
        return False, f"Could not cancel job #{job_id}: {'; '.join(errors)}"

    job["status"] = "cancelled"
    job["aria2_status"] = "removed"
    job["finished_at"] = tb.now_ts()
    return True, f"Cancelled job #{job_id}: {job['name']}"


async def pause_job(job_id: int):
    job = tb.download_jobs.get(job_id)
    if not job:
        return False, f"Job #{job_id} not found."
    if job["status"] not in tb.JOB_ACTIVE_STATES or job["status"] == "paused":
        return False, f"Job #{job_id} cannot be paused from {job['status']}."

    try:
        await tb.aria2_client.pause(job["gid"], force=True)
    except Aria2RpcError as exc:
        return False, f"Could not pause job #{job_id}: {exc}"

    job["status"] = "paused"
    job["aria2_status"] = "paused"
    return True, f"Paused job #{job_id}."


async def resume_job(job_id: int):
    job = tb.download_jobs.get(job_id)
    if not job:
        return False, f"Job #{job_id} not found."
    if job["status"] != "paused":
        return False, f"Job #{job_id} is not paused."

    try:
        await tb.aria2_client.unpause(job["gid"])
    except Aria2RpcError as exc:
        return False, f"Could not resume job #{job_id}: {exc}"

    if job.pop("disk_hold", None):
        # Held for disk space: check again once it is running.
        job["disk_checks"] = [k for k in job.get("disk_checks") or [] if k != f"sized:{job['gid']}"]
        job["last_line"] = ""
    job["status"] = "queued"
    job["aria2_status"] = "waiting"
    return True, f"Resumed job #{job_id}."


def clear_finished_jobs():
    # Mutate in place: the Mini App and dashboard hold references to this dict.
    finished = [
        jid for jid, job in tb.download_jobs.items() if job["status"] not in tb.JOB_ACTIVE_STATES
    ]
    for jid in finished:
        del tb.download_jobs[jid]
    return len(finished)


# =========================================================
# Magnet selective handler
# =========================================================
async def on_torrent_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    torrents_dir = tb.DOWNLOAD_DIR / "_torrents"
    torrents_dir.mkdir(exist_ok=True)

    file = await doc.get_file()
    torrent_path = torrents_dir / Path(doc.file_name or f"upload_{doc.file_unique_id}.torrent").name
    await file.download_to_drive(str(torrent_path))

    return await tb.start_torrent_file_selection(update, context, torrent_path, doc.file_name)


async def start_torrent_file_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    torrent_path: Path,
    title: str | None = None,
):
    files = await asyncio.to_thread(tb.get_torrent_file_list, torrent_path)
    target = update.callback_query.message if update.callback_query else update.message

    if not files:
        await target.reply_text(f"{ICON_FAIL} Could not read the file list of this torrent.")
        return

    user_id = update.effective_user.id
    tb.torrent_select_sessions[user_id] = {
        "torrent_path": str(torrent_path),
        "files": files,
        "selected": set(),
        "page": 0,
        "title": title or torrent_path.name,
    }
    await target.reply_text(
        tb.build_torrent_select_text(user_id),
        reply_markup=tb.build_torrent_select_keyboard(user_id, 0),
    )


def parse_aria2_show_files(output: str) -> list[dict]:
    """Parse ``aria2c --show-files`` output into [{"index", "path", "size"}].

    Each file is printed as ``  1|./path`` followed by ``   |230MiB (241,172,480)``.
    """
    files: list[dict] = []
    for line in output.splitlines():
        if "|" not in line:
            continue
        left, right = line.split("|", 1)
        left = left.strip()
        if left.isdigit():
            files.append({"index": left, "path": right.strip(), "size": ""})
        elif not left and files and not files[-1]["size"]:
            files[-1]["size"] = right.strip().split(" (")[0]
    return files


def get_torrent_file_list(torrent_path: Path):
    try:
        result = subprocess.run(
            [tb.ARIA2_BIN, "--show-files=true", str(torrent_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
    except Exception:
        return []
    return tb.parse_aria2_show_files(result.stdout)


def build_torrent_select_text(user_id: int) -> str:
    session = tb.torrent_select_sessions[user_id]
    total = len(session["files"])
    return (
        f"{ICON_MAGNET} Choose files to download\n\n"
        f"{tb.shorten(tb.clean_download_name(session['title']), 90)}\n"
        f"Selected: {len(session['selected'])} of {total} file(s)"
    )


def build_torrent_select_keyboard(user_id: int, page: int):
    session = tb.torrent_select_sessions[user_id]
    files = session["files"]
    selected = session["selected"]
    pages = max(1, math.ceil(len(files) / tb.TORRENT_FILES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    session["page"] = page
    start = page * tb.TORRENT_FILES_PER_PAGE

    rows = []
    for f in files[start : start + tb.TORRENT_FILES_PER_PAGE]:
        checked = "✅" if f["index"] in selected else "⬜"
        size = f" · {f['size']}" if f.get("size") else ""
        rows.append(
            [
                InlineKeyboardButton(
                    f"{checked} {tb.shorten(Path(f['path']).name, 34)}{size}",
                    callback_data=f"tsel:{f['index']}",
                )
            ]
        )

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"tpage:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"tpage:{page + 1}"))
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton("☑️ Select all", callback_data="tselall"),
            InlineKeyboardButton("⬜ Clear", callback_data="tselnone"),
        ]
    )
    if selected:
        rows.append(
            [
                InlineKeyboardButton(
                    f"✅ Download {len(selected)} selected", callback_data="tconfirm"
                )
            ]
        )
    rows.append([InlineKeyboardButton("📦 Download everything", callback_data="tall")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="tcancel")])
    return InlineKeyboardMarkup(rows)
