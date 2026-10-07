"""/clear and 'Organize videos' through the real bot handlers."""

import pytest

import app.bot.telegram_bot as tb
from tests.fakes import callback_update, make_context, text_update


@pytest.fixture
def jobs(monkeypatch):
    jobs = {
        1: {"id": 1, "name": "done", "status": "completed"},
        2: {"id": 2, "name": "running", "status": "downloading"},
        3: {"id": 3, "name": "broken", "status": "failed"},
    }
    monkeypatch.setattr(tb, "download_jobs", jobs)
    return jobs


@pytest.fixture
def download_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", tmp_path)
    return tmp_path


async def test_clear_command_asks_first_and_never_deletes_files(jobs, download_dir):
    (download_dir / "keep.mkv").write_bytes(b"x")
    context = make_context()

    await tb.clear_jobs_cmd(text_update(context, "/clear"), context)

    reply = context.bot.called("reply_text")[0].kwargs
    assert "Clear 2 finished job(s)" in reply["text"]
    assert reply["reply_markup"] is not None
    assert set(jobs) == {1, 2, 3}
    assert (download_dir / "keep.mkv").exists()


async def test_confirm_clears_only_finished_jobs_in_place(jobs):
    context = make_context()
    original = tb.download_jobs

    await tb.on_button(callback_update(context, "clear_confirm"), context)

    assert tb.download_jobs is original
    assert set(jobs) == {2}
    assert "Cleared 2 finished job(s)" in context.bot.texts()[-1]


async def test_organize_previews_then_moves(jobs, download_dir):
    (download_dir / "Movie").mkdir()
    (download_dir / "Movie" / "Movie.mkv").write_bytes(b"v")
    (download_dir / "Movie" / "notes.txt").write_bytes(b"n")
    context = make_context()

    preview = callback_update(context, "fb:organize:0:")
    await tb.on_button(preview, context)

    text = context.bot.texts()[-1]
    assert "Move 1 video(s)" in text and "kept unless you choose" in text
    assert (download_dir / "Movie" / "Movie.mkv").exists()  # preview changes nothing
    assert len(preview.callback_query.answers) == 1

    await tb.on_button(callback_update(context, "fb:organize_go:0::keep"), context)

    assert (download_dir / "Movie.mkv").exists()
    assert (download_dir / "Movie" / "notes.txt").exists()
    assert "Moved: 1 video(s)" in context.bot.texts()[-1]


async def test_organize_at_root_skips_library_and_active_downloads(jobs, download_dir):
    (download_dir / "Adult" / "Site").mkdir(parents=True)
    (download_dir / "Adult" / "Site" / "v.mp4").write_bytes(b"v")
    (download_dir / "running").mkdir()
    (download_dir / "running" / "r.mp4").write_bytes(b"v")
    context = make_context()

    await tb.on_button(callback_update(context, "fb:organize:0:"), context)

    text = context.bot.texts()[-1]
    assert "Nothing to organize" in text
    assert "Skipped 1 folder(s) with downloads in progress" in text
