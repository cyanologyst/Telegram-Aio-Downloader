"""Disk space checks around downloads (policy in :mod:`app.services.disk_guard`).

Every download is checked before it transfers anything and, for aria2 and
yt-dlp, again once its size is known. Short on space, the oldest finished
files are purged first; if that is not enough, aria2 jobs are held paused
and other jobs fail, both with "Insufficient disk space (needs X, Y free)"
on their card.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from telegram.ext import Application

from app.infrastructure.aria2_rpc import Aria2RpcError
from app.services import disk_guard
from app.bot import telegram_bot as tb


def _disk_lock() -> asyncio.Lock:
    """One check (and purge) at a time; a lock per event loop, like the others."""
    loop = asyncio.get_running_loop()
    lock = tb._disk_locks.get(loop)
    if lock is None:
        lock = tb._disk_locks[loop] = asyncio.Lock()
    return lock


def _as_path(value) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else tb.DOWNLOAD_DIR / path


def disk_protected_paths(extra=()) -> list[Path]:
    """Paths the purge must never touch: configured ones and everything in use.

    Call on the event loop: the job tables change there.
    """
    root = tb.DOWNLOAD_DIR
    found: list = [root / rel for rel in tb.DISK_GUARD_PROTECTED]
    found += [root / name for name in tb.active_job_names() if name]
    for job in list(tb.download_jobs.values()):
        if job.get("status") not in tb.JOB_ACTIVE_STATES:
            continue
        found += [job.get(key) for key in ("filepath", "folder", "tmpfile", "pdf_path")]
        found += list(job.get("files") or []) + list(job.get("outputs") or [])
    for upload in list(tb.upload_jobs.values()):
        if upload.get("status") == "uploading":
            found += list(upload.get("files") or [])
    for job in list(tb.compress_jobs.values()):
        if job.get("status") in ("queued", "encoding"):
            found += [job.get("source"), job.get("output")]
    found += list(extra)
    return [p for p in (_as_path(item) for item in found) if p is not None]


async def _aria2_paths() -> list[str]:
    """Files of every unfinished aria2 download, including ones the bot never saw."""
    try:
        statuses = await tb.unfinished_aria2_downloads()
    except Exception as exc:
        tb.logger.debug("Disk guard could not list aria2 downloads: %s", exc)
        return []
    return [f["path"] for s in statuses for f in s.get("files") or [] if f.get("path")]


async def check_disk_space(estimated: int | None = None, extra_protected=()) -> disk_guard.SpaceCheck | None:
    """Free ``estimated`` + buffer bytes (purging if allowed); None when the guard is off."""
    if not tb.DISK_GUARD_ENABLE:
        return None
    async with _disk_lock():
        protected = tb.disk_protected_paths(list(extra_protected) + await _aria2_paths())
        check = await asyncio.to_thread(
            disk_guard.ensure_space,
            tb.DOWNLOAD_DIR,
            estimated,
            buffer=tb.DISK_GUARD_BUFFER_GB * disk_guard.GB,
            protected=protected,
            auto_purge=tb.DISK_GUARD_AUTO_PURGE,
            keep_dirs=[tb.DOWNLOAD_DIR / name for name in tb.KEPT_FOLDERS],
        )
    note = check.purge_note()
    if note:
        tb.logger.info("Disk guard: %s", note)
    if not check.ok:
        tb.logger.warning("Disk guard: %s", check.message())
    return check


def _record_purge(job: dict, check: disk_guard.SpaceCheck | None) -> None:
    note = check.purge_note() if check else None
    if note:
        job["disk_note"] = f"🧹 {note}"


async def ensure_job_disk_space(app: Application, job: dict, estimated: int | None = None) -> bool:
    """Gate for a download about to start; on False the job is failed and its card finished."""
    check = await tb.check_disk_space(estimated)
    _record_purge(job, check)
    if check is None or check.ok:
        return True
    job["status"] = "failed"
    job["finished_at"] = tb.now_ts()
    job["last_line"] = check.message()
    await tb.maybe_auto_update_status_message(app, job, force=True)
    await tb.finish_job_card(app, job)
    return False


def ytdlp_disk_check(job: dict, total: int, downloaded: int, loop: asyncio.AbstractEventLoop) -> str | None:
    """From yt-dlp's progress hook (a worker thread): the refusal reason, or None.

    Runs once per job, when the size first becomes known.
    """
    if not tb.DISK_GUARD_ENABLE or not total or job.get("disk_checked_size"):
        return None
    job["disk_checked_size"] = True
    remaining = max(0, int(total) - int(downloaded or 0))
    check = asyncio.run_coroutine_threadsafe(tb.check_disk_space(remaining), loop).result()
    _record_purge(job, check)
    if check is None or check.ok:
        return None
    return check.message()


async def hold_aria2_job_for_disk(app: Application, job: dict, check: disk_guard.SpaceCheck) -> None:
    """Pause an aria2 job that does not fit; the card says why, Resume retries it."""
    try:
        await tb.aria2_client.pause(job["gid"], force=True)
    except Aria2RpcError as exc:
        tb.logger.debug("Could not pause job %s for disk space: %s", job.get("id"), exc)
    job["status"] = "paused"
    job["aria2_status"] = "paused"
    job["disk_hold"] = check.message()
    job["last_line"] = check.message()
    await tb.maybe_auto_update_status_message(app, job, force=True)


async def aria2_disk_gate(app: Application, job: dict, status: dict) -> bool:
    """Re-check an aria2 job once its size is known (per GID, so a magnet's
    metadata download does not stand in for the real one). True if it was held.
    """
    if not tb.DISK_GUARD_ENABLE or job.get("status") != "downloading":
        return False
    total = int(job.get("total_length") or 0)
    gid = status.get("gid") or job.get("gid")
    key = f"sized:{gid}"
    if not total or key in (job.get("disk_checks") or []):
        return False
    job.setdefault("disk_checks", []).append(key)
    paths = [Path(p) for p in job.get("files") or []]
    # Space aria2 already preallocated is taken; only the rest is still needed.
    remaining = max(0, total - disk_guard.allocated_bytes(paths))
    check = await tb.check_disk_space(remaining, extra_protected=paths)
    _record_purge(job, check)
    if check is None or check.ok:
        return False
    await tb.hold_aria2_job_for_disk(app, job, check)
    return True
