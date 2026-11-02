"""Saving and restoring jobs across restarts."""

from __future__ import annotations

import asyncio

from telegram.ext import Application

from app.infrastructure.aria2_rpc import Aria2RpcError
from app.bot import telegram_bot as tb


def save_jobs_now() -> None:
    if tb.job_store is not None:
        tb.job_store.sync([dict(job) for job in tb.download_jobs.values()])


async def job_persistence_loop():
    last_fingerprint = None
    while True:
        await asyncio.sleep(tb.JOB_SAVE_INTERVAL)
        try:
            snapshot = [dict(job) for job in tb.download_jobs.values()]
            # Skip the write (and its fsync) when nothing changed since the last save.
            fingerprint = tb._hash_content(repr([sorted(map(str, job.items())) for job in snapshot]))
            if fingerprint == last_fingerprint:
                continue
            await asyncio.to_thread(tb.job_store.sync, snapshot)
            last_fingerprint = fingerprint
        except Exception:
            tb.logger.exception("Saving jobs failed")


async def reattach_aria2_job(app: Application, job: dict) -> None:
    """Pick a torrent/direct download back up after the bot restarted.

    aria2 keeps its own session file, so the download is usually still there
    under the same GID; otherwise it is found by info hash or added again
    (aria2 resumes from the partial files on disk).
    """
    try:
        await tb.aria2_client.ensure_started()
        status = None
        for gid in dict.fromkeys([job.get("gid"), *reversed(job.get("gid_history") or [])]):
            if not gid:
                continue
            try:
                status = await tb.aria2_client.tell_status(gid)
                break
            except Aria2RpcError:
                continue
        if status is None and job.get("info_hash"):
            status = await tb.find_aria2_status_by_info_hash(job["info_hash"])
        if status is not None:
            job["gid"] = status.get("gid", job.get("gid"))
            if status.get("status") == "paused" and job.get("status") != "paused":
                job["slot_held"] = True  # paused by the download queue, not the user
        elif job.get("source"):
            job["gid"], _, _ = await tb.aria2_add_source(job["source"])
        else:
            raise RuntimeError("aria2 no longer knows this download")
        job["monitor_errors"] = 0
        job["note"] = "resumed after a restart"
        tb.run_in_background(tb.monitor_aria2_job(app, job["id"]), name=f"aria2-{job['id']}")
    except Exception as exc:
        tb.logger.warning("Could not reattach job %s: %s", job.get("id"), exc)
        job["status"] = "failed"
        job["last_line"] = f"Could not resume after the bot restarted: {exc}"
        job["finished_at"] = tb.now_ts()
        await tb.finish_job_card(app, job)


async def restore_jobs(app: Application) -> None:
    """Load saved jobs: keep finished ones, resume aria2 ones, mark the rest interrupted."""
    try:
        saved = await asyncio.to_thread(tb.job_store.load)
    except Exception:
        tb.logger.exception("Loading saved jobs failed")
        return
    for job in saved:
        tb.download_jobs[job["id"]] = job
        tb.job_counter = max(tb.job_counter, int(job["id"]))
    for job in saved:
        if job.get("status") not in tb.JOB_ACTIVE_STATES:
            continue
        if not job.get("provider") and (job.get("gid") or job.get("source")):
            await tb.reattach_aria2_job(app, job)
        else:
            # yt-dlp/Spotify/manga run inside the bot process and stopped with it.
            job["status"] = "failed"
            job["last_line"] = "Interrupted by a bot restart. Tap Retry to resume it."
            job["finished_at"] = tb.now_ts()
            await tb.finish_job_card(app, job)
    if saved:
        tb.logger.info("Restored %d saved job(s)", len(saved))
