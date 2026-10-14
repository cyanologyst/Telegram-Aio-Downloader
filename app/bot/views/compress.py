"""The card of one Compress (H.265) job: progress, then the result and its buttons."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from telegram import InlineKeyboardMarkup

from app.bot.views.common import Screen, back_row, button, e, human_size, progress_bar
from app.bot.views.jobs import duration

Tok = Callable[[str], str]


def _settings_line(job: dict[str, Any]) -> str:
    return f"{job.get('codec', 'H.265')} · CRF {job.get('crf')} · {job.get('preset')}"


def size_change(before: int, after: int) -> str:
    """"1.2 GB → 500.0 MB (−58%)"."""
    line = f"{human_size(before)} → {human_size(after)}"
    if before > 0:
        change = round((after - before) / before * 100)
        line += f" ({'+' if change > 0 else '−' if change < 0 else '±'}{abs(change)}%)"
    return line


def compress_card(job: dict[str, Any], tok: Tok, *, confirm_replace: bool = False) -> Screen:
    cid = job["id"]
    status = job.get("status")
    name = e(Path(job["source"]).name)
    back = back_row(job.get("back"))
    cancel = [button("✖ Cancel", f"cmp:cancel:{cid}")]

    if status == "queued":
        text = (
            f"🗜 <b>Compress</b> {name}\n{_settings_line(job)}\n\n"
            "Waiting for the compression before it to finish…"
        )
        return text, InlineKeyboardMarkup([cancel])

    if status == "encoding":
        pct = int(job.get("pct") or 0)
        elapsed = duration(time.time() - job["started_at"]) if job.get("started_at") else "?"
        text = (
            f"🗜 <b>Compressing</b> {name}\n{_settings_line(job)}\n\n"
            f"{progress_bar(pct / 100)} {pct}%\n⏱ {elapsed}"
        )
        return text, InlineKeyboardMarkup([cancel])

    if status in ("done", "replaced"):
        took = ""
        if job.get("started_at") and job.get("finished_at"):
            took = f" · took {duration(job['finished_at'] - job['started_at'])}"
        lines = [
            f"✅ <b>Compressed</b> {name}",
            _settings_line(job) + took,
            size_change(int(job.get("original_size") or 0), int(job.get("new_size") or 0)),
        ]
        output = job["output"]
        if status == "replaced":
            lines.append(f"Replaced the original: <code>{e(Path(output).name)}</code>")
        else:
            lines.append(f"Saved as <code>{e(Path(output).name)}</code>")
            if int(job.get("new_size") or 0) >= int(job.get("original_size") or 0):
                lines.append("⚠️ It came out bigger than the original.")
        text = "\n".join(lines)
        if confirm_replace:
            rows = [[
                button("♻️ Yes, replace it", f"cmp:repy:{cid}"),
                button("Keep both", f"cmp:repn:{cid}"),
            ]]
            return text + "\n\nDelete the original and keep only the compressed file?", InlineKeyboardMarkup(rows)
        first = [button("📤 Upload", f"cmp:up:{cid}")]
        if status == "done":
            first.append(button("♻️ Replace original", f"cmp:rep:{cid}"))
        rows = [first, [button("📁 Open", f"fb:file:0:{tok(output)}")], back]
        return text, InlineKeyboardMarkup(rows)

    if status == "failed":
        error = e(job.get("error") or "Unknown error")
        return f"❌ <b>Compression failed</b> {name}\n\n{error}", InlineKeyboardMarkup([back])

    return f"🛑 <b>Compression cancelled</b> {name}", InlineKeyboardMarkup([back])
