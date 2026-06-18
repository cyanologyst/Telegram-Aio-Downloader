import pytest

from app.services.batch_download import (
    BatchDownloadMode,
    BatchProgress,
    batch_download_mode_description,
    batch_download_mode_label,
    normalize_batch_download_mode,
    run_sequential_batch,
)


def test_normalize_batch_download_mode_defaults_to_download_only():
    assert normalize_batch_download_mode(None) is BatchDownloadMode.DOWNLOAD_ONLY
    assert normalize_batch_download_mode("unknown") is BatchDownloadMode.DOWNLOAD_ONLY


def test_batch_download_mode_labels_and_descriptions():
    mode = BatchDownloadMode.UPLOAD_AND_DELETE

    assert batch_download_mode_label(mode) == "Upload & delete each"
    assert "Saved Messages" in batch_download_mode_description(mode)
    assert batch_download_mode_label(BatchDownloadMode.DOWNLOAD_ONLY) == "Download only"


@pytest.mark.asyncio
async def test_run_sequential_batch_finishes_each_item_before_next():
    events = []
    progress_updates: list[BatchProgress] = []

    async def process_item(item, index, total):
        events.append(("download", item))
        return f"{item}.mp4"

    async def after_item(result, index, total):
        events.append(("upload", result))

    async def on_progress(progress):
        progress_updates.append(progress)

    completed = await run_sequential_batch(
        ["one", "two"],
        process_item,
        after_item=after_item,
        on_progress=on_progress,
    )

    assert events == [
        ("download", "one"),
        ("upload", "one.mp4"),
        ("download", "two"),
        ("upload", "two.mp4"),
    ]
    assert completed == 2
    assert progress_updates[-1].completed == 2
    assert progress_updates[-1].remaining == 0


@pytest.mark.asyncio
async def test_run_sequential_batch_stops_after_cancellation():
    events = []
    cancelled = False

    async def process_item(item, index, total):
        nonlocal cancelled
        events.append(item)
        cancelled = True
        return item

    completed = await run_sequential_batch(
        ["one", "two"],
        process_item,
        is_cancelled=lambda: cancelled,
    )

    assert events == ["one"]
    assert completed == 0
