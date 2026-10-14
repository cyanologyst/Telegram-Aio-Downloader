"""Refuse to upload files that are still being downloaded.

A torrent file aria2 is still fetching is full of holes (and an MP4 usually
lacks its moov atom until the end), so sending it gives a broken video. Two
checks, cheapest first:

1. aria2: the file belongs to an active or waiting download and isn't
   complete yet (a finished file of a still-running torrent is fine).
2. For videos ffprobe can't read: the file was modified moments ago, or its
   size/mtime changes between two quick looks. A video that is unreadable but
   stable is let through; the upload's metadata fallback copes with it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from app.services import upload_filter

logger = logging.getLogger(__name__)

RECENT_WRITE_SECONDS = 30.0  # an unreadable video modified this recently is still being written
STABILITY_WAIT = 1.5  # seconds between the two looks at an unreadable video

StatusFetcher = Callable[[], Awaitable[Iterable[dict[str, Any]]]]
Probe = Callable[[Path], Awaitable[bool]]


def refusal_text(percent: float | None) -> str:
    detail = f"{percent:.1f}% complete" if percent is not None else "still being written"
    return f"Refusing: file still downloading ({detail}), try after completion"


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _real(path: str | Path) -> str:
    return os.path.realpath(str(path))


def aria2_unfinished_percent(path: str | Path, statuses: Iterable[dict[str, Any]]) -> float | None:
    """How far aria2 is with ``path``, or None if no unfinished download has it.

    ``statuses`` are tellActive/tellWaiting entries (with their ``files``).
    """
    target = _real(path)
    for status in statuses:
        for entry in status.get("files") or []:
            file_path = entry.get("path")
            if not file_path or _real(file_path) != target:
                continue
            length = _int(entry.get("length"))
            done = _int(entry.get("completedLength"))
            if length and done >= length:
                continue  # this file is finished even if the rest of the torrent isn't
            return (done / length * 100) if length else 0.0
    return None


def _snapshot(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


async def check_upload(
    path: str | Path,
    *,
    fetch_statuses: StatusFetcher,
    probe_readable: Probe,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> str | None:
    """Why ``path`` must not be uploaded yet, or None if it may be."""
    path = Path(path)
    try:
        statuses = list(await fetch_statuses())
    except Exception as exc:
        # aria2 down or restarting: its control file still marks an unfinished download.
        logger.debug("aria2 not asked about %s: %s", path.name, exc)
        if path.with_name(path.name + ".aria2").exists():
            return refusal_text(None)
    else:
        percent = aria2_unfinished_percent(path, statuses)
        if percent is not None:
            return refusal_text(percent)

    if not upload_filter.is_video(path) or await probe_readable(path):
        return None
    first = _snapshot(path)
    if first is None:
        return None  # gone: the upload itself reports that
    if now() - first[1] / 1e9 < RECENT_WRITE_SECONDS:
        return refusal_text(None)
    await sleep(STABILITY_WAIT)
    if _snapshot(path) != first:
        return refusal_text(None)
    return None
