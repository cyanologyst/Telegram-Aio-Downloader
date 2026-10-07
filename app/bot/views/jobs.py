"""Live job cards and the status dashboard.

Every download gets one card that moves through its states: progress with
Pause/Cancel while it runs, then a result with Upload / Open / Delete (or
Retry after a failure). Callback data: ``job:<action>:<job id>``.
"""

from __future__ import annotations

from typing import Any

from telegram import InlineKeyboardMarkup

from app.bot.views.common import Screen, button, e, home_button, human_size, progress_bar, short
from app.services.batch_download import BatchDownloadMode, normalize_batch_download_mode

ACTIVE = {
    "starting",
    "downloading",
    "uploading",
    "metadata",
    "allocating",
    "queued",
    "paused",
    "processing",
}
BATCH_PROVIDERS = {"hentai-playlist", "pornhub-model"}

ENGINE_LABELS = {
    "yt-dlp": "Video",
    "spotify": "Spotify",
    "manga": "Gallery",
    "hentai-playlist": "Playlist",
    "pornhub-model": "Model page",
}

STATE_LINES = {
    "starting": "Starting…",
    "metadata": "Fetching torrent metadata…",
    "queued": "Queued",
    "allocating": "Allocating disk space…",
    "processing": "Finishing up…",
    "paused": "⏸ Paused",
}


def duration(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0:
        return "?"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def engine_label(job: dict[str, Any]) -> str:
    provider = job.get("provider")
    if provider in ENGINE_LABELS:
        label = ENGINE_LABELS[provider]
        if provider == "yt-dlp" and job.get("platform"):
            label = str(job["platform"])
        if job.get("audio_only"):
            label += " · MP3"
        elif job.get("max_height"):
            label += f" · ≤{job['max_height']}p"
        return label
    return {"magnet": "Torrent", "torrent": "Torrent", "http": "Direct link"}.get(
        str(job.get("source_type")), "Download"
    )


def is_active(job: dict[str, Any]) -> bool:
    return job.get("status") in ACTIVE


def _title(job: dict[str, Any], limit: int = 80) -> str:
    return e(short(job.get("name") or "Download", limit))


def _progress_lines(job: dict[str, Any]) -> list[str]:
    status = job.get("status")
    if job.get("provider") in BATCH_PROVIDERS:
        total = int(job.get("episode_count") or job.get("video_count") or 0)
        done = int(job.get("completed_items") or 0)
        lines = [f"{progress_bar(done / total if total else 0)} {done}/{total} done"]
        if job.get("last_line"):
            lines.append(e(short(job["last_line"], 90)))
        return lines

    total = int(job.get("total_length") or 0)
    done = int(job.get("completed_length") or 0)
    lines = []
    if total:
        lines.append(f"{progress_bar(done / total)} {done / total * 100:.0f}%")
        details = [f"{human_size(done)} of {human_size(total)}"]
    elif done:
        details = [f"{human_size(done)} so far"]
    else:
        details = []
    speed = int(job.get("download_speed") or 0)
    if speed and status == "downloading":
        details.append(f"{human_size(speed)}/s")
    eta = job.get("eta")
    if status == "downloading" and eta and eta != "Unknown":
        details.append(f"ETA {e(eta)}")
    if details:
        lines.append(" · ".join(details))
    if status in STATE_LINES:
        lines.append(STATE_LINES[status])
    elif status == "uploading" and job.get("last_line"):
        lines.append(e(short(job["last_line"], 90)))
    if job.get("source_type") in ("magnet", "torrent") and status == "downloading":
        lines.append(f"Peers {job.get('connections', 0)} · seeders {job.get('num_seeders', 0)}")
    elif job.get("provider") in ("spotify", "manga") and job.get("last_line"):
        lines.append(e(short(job["last_line"], 90)))
    return lines


def job_card(
    job: dict[str, Any],
    *,
    open_data: str | None = None,
    can_upload: bool = False,
    confirm: str | None = None,
) -> Screen:
    """The card for one job. ``confirm`` is "cancel" or "delete" while asking."""
    jid = job["id"]
    status = job.get("status")
    header = f"#{jid} · {e(engine_label(job))}"

    if job.get("note"):
        header += f" · {e(job['note'])}"

    if is_active(job):
        text = "\n".join([f"📥 <b>{_title(job)}</b>", header, "", *_progress_lines(job)])
        if confirm == "cancel":
            rows = [
                [
                    button("🛑 Yes, cancel it", f"job:cy:{jid}"),
                    button("Keep going", f"job:cn:{jid}"),
                ]
            ]
            return text + "\n\nCancel this download?", InlineKeyboardMarkup(rows)
        controls = []
        if not job.get("provider"):  # aria2 can pause
            if status == "paused":
                controls.append(button("▶ Resume", f"job:resume:{jid}"))
            else:
                controls.append(button("⏸ Pause", f"job:pause:{jid}"))
        controls.append(button("✖ Cancel", f"job:cc:{jid}"))
        return text, InlineKeyboardMarkup([controls])

    took = ""
    if job.get("started_at") and job.get("finished_at"):
        took = f" in {duration(job['finished_at'] - job['started_at'])}"

    if status == "completed":
        lines = [f"✅ <b>{_title(job)}</b>", header + f" · done{took}"]
        size = int(job.get("total_length") or job.get("completed_length") or 0)
        if size:
            lines.append(human_size(size))
        if job.get("provider") in BATCH_PROVIDERS:
            mode = normalize_batch_download_mode(job.get("batch_download_mode"))
            if mode is BatchDownloadMode.UPLOAD_AND_DELETE:
                lines.append(f"Uploaded and removed: {job.get('uploaded_items', 0)} item(s)")
            else:
                lines.append(f"Downloaded: {job.get('completed_items', 0)} item(s)")
        if job.get("image_count"):
            lines.append(
                f"{job['image_count']} images" + (" · PDF made" if job.get("pdf_path") else "")
            )
        if job.get("auto_upload_note"):
            lines.append(e(job["auto_upload_note"]))
        text = "\n".join(lines)
        if confirm == "delete":
            rows = [
                [
                    button("🗑 Yes, delete the files", f"job:dely:{jid}"),
                    button("Keep them", f"job:deln:{jid}"),
                ]
            ]
            return text + "\n\nDelete the downloaded files from the server?", InlineKeyboardMarkup(
                rows
            )
        actions = []
        if can_upload:
            actions.append(button("📤 Upload", f"job:up:{jid}"))
        if open_data:
            actions.append(button("📁 Open", open_data))
        if can_upload:
            actions.append(button("🗑 Delete", f"job:del:{jid}"))
        return text, InlineKeyboardMarkup([actions]) if actions else None

    if status == "failed":
        reason = e(short(job.get("last_line") or "Unknown error", 300))
        text = f"❌ <b>{_title(job)}</b>\n{header} · failed{took}\n\n{reason}"
        rows = [[button("🔁 Retry", f"job:retry:{jid}"), button("✖ Dismiss", f"job:dismiss:{jid}")]]
        return text, InlineKeyboardMarkup(rows)

    # cancelled (or anything unexpected)
    return f"🛑 <b>{_title(job)}</b>\n{header} · cancelled", None


def status_screen(jobs: list[dict[str, Any]], free_bytes: int | None, *, recent: int = 5) -> Screen:
    active = sorted((j for j in jobs if is_active(j)), key=lambda j: j["id"])
    finished = sorted(
        (j for j in jobs if not is_active(j)),
        key=lambda j: j.get("finished_at") or 0,
        reverse=True,
    )[:recent]

    summary = f"⬇️ {len(active)} active" if active else "⬇️ Nothing downloading"
    if free_bytes is not None:
        summary += f" · 💽 {human_size(free_bytes)} free"
    lines = ["📊 <b>Status</b>", "", summary]

    if active:
        lines.append("")
        for job in active:
            total = int(job.get("total_length") or 0)
            done = int(job.get("completed_length") or 0)
            if job.get("provider") in BATCH_PROVIDERS:
                count = int(job.get("episode_count") or job.get("video_count") or 0)
                state = f"{job.get('completed_items', 0)}/{count} items"
            elif job.get("status") in STATE_LINES:
                state = STATE_LINES[job["status"]].rstrip("…")
            elif total:
                state = f"{done / total * 100:.0f}%"
                speed = int(job.get("download_speed") or 0)
                if speed:
                    state += f" · {human_size(speed)}/s"
            else:
                state = "working"
            lines.append(f"<b>#{job['id']}</b> {_title(job, 50)}\n      {e(state)}")

    if finished:
        lines += ["", "<b>Recently finished</b>"]
        icons = {"completed": "✅", "failed": "❌", "cancelled": "🛑"}
        for job in finished:
            lines.append(f"{icons.get(str(job.get('status')), '•')} #{job['id']} {_title(job, 50)}")

    rows = []
    jump = [button(f"#{job['id']}", f"job:show:{job['id']}") for job in active[:8]]
    if jump:
        rows += [jump[i : i + 4] for i in range(0, len(jump), 4)]
    bottom = [button("🔄 Refresh", "nav:status")]
    if finished:
        bottom.append(button("🧹 Clear finished", "menu:clear"))
    bottom.append(home_button())
    rows.append(bottom)
    if active:
        lines += ["", "Tap a job number to bring its card down here."]
    return "\n".join(lines), InlineKeyboardMarkup(rows)
