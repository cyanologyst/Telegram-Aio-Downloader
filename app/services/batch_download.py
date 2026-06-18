"""Shared batch-download mode helpers."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")


class BatchDownloadMode(StrEnum):
    """Supported storage strategies for multi-item downloads."""

    UPLOAD_AND_DELETE = "upload_and_delete"
    DOWNLOAD_ONLY = "download_only"


DEFAULT_BATCH_DOWNLOAD_MODE = BatchDownloadMode.DOWNLOAD_ONLY


@dataclass(frozen=True, slots=True)
class BatchProgress:
    """Provider-neutral progress for a sequential batch."""

    current: int
    total: int
    completed: int
    remaining: int
    phase: str


def normalize_batch_download_mode(value: object) -> BatchDownloadMode:
    """Return a valid batch mode, falling back to download-only."""
    try:
        return BatchDownloadMode(str(value))
    except ValueError:
        return DEFAULT_BATCH_DOWNLOAD_MODE


def batch_download_mode_label(value: object) -> str:
    """Return a short label suitable for settings and status messages."""
    mode = normalize_batch_download_mode(value)
    if mode is BatchDownloadMode.UPLOAD_AND_DELETE:
        return "Upload & delete each"
    return "Download only"


def batch_download_mode_description(value: object) -> str:
    """Explain the behavior of a batch mode."""
    mode = normalize_batch_download_mode(value)
    if mode is BatchDownloadMode.UPLOAD_AND_DELETE:
        return "Download one item, upload it to Saved Messages, delete it, then continue."
    return "Download every item to the VPS without automatic upload or deletion."


async def run_sequential_batch(
    items: Sequence[ItemT],
    process_item: Callable[[ItemT, int, int], Awaitable[ResultT]],
    *,
    after_item: Callable[[ResultT, int, int], Awaitable[None]] | None = None,
    on_progress: Callable[[BatchProgress], Awaitable[None]] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> int:
    """Process each item fully before starting the next one."""
    total = len(items)
    completed = 0

    for index, item in enumerate(items, start=1):
        if is_cancelled and is_cancelled():
            break
        if on_progress:
            await on_progress(
                BatchProgress(
                    current=index,
                    total=total,
                    completed=completed,
                    remaining=total - completed,
                    phase="processing",
                )
            )

        result = await process_item(item, index, total)
        if is_cancelled and is_cancelled():
            break
        if after_item:
            await after_item(result, index, total)
        if is_cancelled and is_cancelled():
            break

        completed = index
        if on_progress:
            await on_progress(
                BatchProgress(
                    current=index,
                    total=total,
                    completed=completed,
                    remaining=total - completed,
                    phase="completed",
                )
            )

    return completed
