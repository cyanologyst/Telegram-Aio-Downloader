"""Live job cards: rendering, lifecycle and buttons."""

import asyncio

import pytest

import app.bot.telegram_bot as tb
from app.bot.views import jobs as job_views
from app.services import user_settings
from tests.fakes import callback_update, make_context


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "Download"
    root.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", root)
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "status_messages", {})
    return root


def _job(**overrides):
    job = {
        "id": 7,
        "name": "Ubuntu <24.04>.iso",
        "status": "downloading",
        "chat_id": 1,
        "user_id": 1,
        "source_type": "magnet",
        "total_length": 4 * 1024**3,
        "completed_length": 1024**3,
        "download_speed": 5 * 1024**2,
        "eta": "10m",
        "connections": 12,
        "num_seeders": 30,
        "started_at": 1000,
        "finished_at": None,
    }
    job.update(overrides)
    tb.download_jobs[job["id"]] = job
    return job


def _data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


async def _drain():
    while tb.background_tasks:
        await asyncio.gather(*list(tb.background_tasks), return_exceptions=True)
        await asyncio.sleep(0)  # let done-callbacks remove finished tasks


# ---------------------------------------------------------------- views


def test_active_card_shows_progress_and_controls():
    text, markup = job_views.job_card(_job())

    assert "📥 <b>Ubuntu &lt;24.04&gt;.iso</b>" in text
    assert "#7 · Torrent" in text
    assert "25%" in text and "1.0 GB of 4.0 GB" in text and "5.0 MB/s" in text
    assert "Peers 12 · seeders 30" in text
    assert _data(markup) == ["job:pause:7", "job:cc:7"]


def test_paused_and_non_aria2_controls():
    _, paused = job_views.job_card(_job(status="paused"))
    assert _data(paused)[0] == "job:resume:7"
    _, video = job_views.job_card(_job(provider="yt-dlp", platform="YouTube", max_height=720))
    assert _data(video) == ["job:cc:7"]
    assert (
        "#7 · YouTube · ≤720p"
        in job_views.job_card(_job(provider="yt-dlp", platform="YouTube", max_height=720))[0]
    )


def test_finished_cards():
    done_text, done = job_views.job_card(
        _job(status="completed", finished_at=1000 + 125), open_data="fb:list:0:", can_upload=True
    )
    assert done_text.startswith("✅") and "done in 2m 5s" in done_text
    assert _data(done) == ["job:up:7", "fb:list:0:", "job:del:7"]

    failed_text, failed = job_views.job_card(_job(status="failed", last_line="Tracker <error>"))
    assert "Tracker &lt;error&gt;" in failed_text
    assert _data(failed) == ["job:retry:7", "job:dismiss:7"]

    cancelled_text, cancelled = job_views.job_card(_job(status="cancelled"))
    assert cancelled_text.startswith("🛑") and cancelled is None


def test_status_screen_lists_active_and_recent():
    jobs = [
        _job(),
        _job(id=8, name="Done", status="completed", finished_at=5),
        _job(id=9, name="Broke", status="failed", finished_at=6),
    ]
    text, markup = job_views.status_screen(jobs, 10 * 1024**3)

    assert "⬇️ 1 active · 💽 10.0 GB free" in text
    assert "#7" in text and "25% · 5.0 MB/s" in text
    assert "❌ #9 Broke" in text and "✅ #8 Done" in text
    assert "job:show:7" in _data(markup) and "menu:clear" in _data(markup)


# ---------------------------------------------------------------- lifecycle


async def test_card_is_sent_then_edited_then_replaced_when_done(isolated):
    job = _job()
    context = make_context()
    app = context.application

    await tb.attach_job_card(app, job)
    first_id = job["card"]["message_id"]
    assert context.bot.calls[-1].method == "send_message"

    job["completed_length"] = 2 * 1024**3
    await tb.refresh_job_card(app, job)  # throttled: too soon
    assert len(context.bot.calls) == 1
    await tb.refresh_job_card(app, job, force=True)
    assert context.bot.calls[-1].method == "edit_message_text"
    assert "50%" in context.bot.calls[-1].kwargs["text"]

    (isolated / "ubuntu.iso").write_bytes(b"iso")
    job.update(status="completed", finished_at=2000, files=[str(isolated / "ubuntu.iso")])
    await tb.finish_job_card(app, job)

    final = context.bot.called("send_message")[-1]
    assert final.kwargs["text"].startswith("✅")
    assert "job:up:7" in _data(final.kwargs["reply_markup"])
    deleted = context.bot.called("delete_message")
    assert deleted[-1].kwargs["message_id"] == first_id

    await tb.finish_job_card(app, job)  # idempotent
    assert len(context.bot.called("send_message")) == 2


async def test_prompt_becomes_the_card():
    job = _job(provider="yt-dlp", platform="YouTube")
    context = make_context()
    prompt = callback_update(context, "x").callback_query.message

    await tb.attach_job_card(context.application, job, message=prompt)

    assert context.bot.calls[-1].method == "edit_text"
    assert job["card"]["message_id"] == prompt.message_id


async def test_cancel_asks_first_then_finalises_in_place(monkeypatch):
    job = _job(provider="yt-dlp")
    context = make_context()
    await tb.attach_job_card(context.application, job)

    await tb.handle_job_callback(callback_update(context, "job:cc:7"), context)
    assert "Cancel this download?" in context.bot.calls[-1].kwargs["text"]
    assert job["status"] == "downloading"

    update = callback_update(context, "job:cy:7")
    await tb.handle_job_callback(update, context)

    assert job["status"] == "cancelled" and job["card_final"] == "cancelled"
    assert context.bot.calls[-1].kwargs["text"].startswith("🛑")


async def test_upload_and_delete_buttons_use_the_job_outputs(isolated, monkeypatch):
    (isolated / "Show").mkdir()
    for name in ("e1.mkv", "e2.mkv"):
        (isolated / "Show" / name).write_bytes(b"v")
    job = _job(
        status="completed",
        finished_at=2000,
        files=[str(isolated / "Show" / "e1.mkv"), str(isolated / "Show" / "e2.mkv")],
    )
    uploads = []

    async def fake_upload(app, chat_id, message_id, files, *, title, user_id=None):
        uploads.append(sorted(files))

    monkeypatch.setattr(tb, "upload_files_via_pyrogram", fake_upload)
    context = make_context()

    text, markup = tb.render_job_card(job)
    assert f"fb:list:0:{tb.encode_path('Show')}" in _data(markup)  # Open → the common folder

    await tb.handle_job_callback(callback_update(context, "job:up:7"), context)
    await _drain()
    assert uploads == [["Show/e1.mkv", "Show/e2.mkv"]]

    await tb.handle_job_callback(callback_update(context, "job:del:7"), context)
    assert "Delete the downloaded files" in context.bot.calls[-1].kwargs["text"]
    await tb.handle_job_callback(callback_update(context, "job:dely:7"), context)
    assert not (isolated / "Show").exists()  # emptied folder removed too
    assert "files deleted" in context.bot.calls[-1].kwargs["text"]


async def test_auto_upload_when_enabled(isolated, monkeypatch):
    await user_settings.update_setting(1, "auto_upload_after_download", True)
    (isolated / "clip.mp4").write_bytes(b"v")
    uploads = []

    async def fake_upload(app, chat_id, message_id, files, *, title, user_id=None):
        uploads.append(files)

    monkeypatch.setattr(tb, "upload_files_via_pyrogram", fake_upload)
    job = _job(
        provider="yt-dlp", status="completed", finished_at=2000, filepath=str(isolated / "clip.mp4")
    )
    context = make_context()

    await tb.finish_job_card(context.application, job)
    await _drain()

    assert uploads == [["clip.mp4"]]
    assert "Uploading to Saved Messages" in context.bot.called("send_message")[0].kwargs["text"]


async def test_retry_restarts_the_same_download(monkeypatch):
    job = _job(
        provider="yt-dlp", status="failed", url="https://youtu.be/x", audio_only=True, finished_at=5
    )
    restarted = []

    async def fake_ytdlp(
        app, chat_id, url, audio_only=False, user_id=None, max_height=None, **kwargs
    ):
        restarted.append((url, audio_only))
        new = _job(id=8, provider="yt-dlp", status="starting")
        return new

    monkeypatch.setattr(tb, "start_ytdlp_download", fake_ytdlp)
    context = make_context()
    await tb.attach_job_card(context.application, job)
    update = callback_update(context, "job:retry:7")
    update.callback_query.message.message_id = job["card"]["message_id"]

    await tb.handle_job_callback(update, context)

    assert restarted == [("https://youtu.be/x", True)]
    assert tb.download_jobs[8]["card"]["message_id"] == job["card"]["message_id"]


async def test_show_moves_the_card_down(monkeypatch):
    job = _job()
    context = make_context()
    await tb.attach_job_card(context.application, job)
    first = job["card"]["message_id"]

    await tb.handle_job_callback(callback_update(context, "job:show:7"), context)

    assert job["card"]["message_id"] != first
    assert context.bot.called("delete_message")[-1].kwargs["message_id"] == first


def test_aria2_status_records_selected_files():
    job = _job()
    tb._apply_aria2_status(
        job,
        {
            "status": "active",
            "totalLength": "10",
            "completedLength": "5",
            "files": [
                {"path": "/d/a.mkv", "selected": "true"},
                {"path": "/d/b.nfo", "selected": "false"},
            ],
        },
    )
    assert job["files"] == ["/d/a.mkv"]


def test_quality_actions_hide_caps_the_video_cannot_use():
    assert [a for _, a in tb.quality_actions([360, 720], 600)] == ["best", "h480", "mp3"]
    assert [a for _, a in tb.quality_actions([2160, 1080], 600)] == [
        "best",
        "h1080",
        "h720",
        "h480",
        "mp3",
    ]
    assert [a for _, a in tb.quality_actions([], 600)] == ["best", "h720", "mp3"]
    # Clips of up to a minute (or of unknown length) can be sent as a GIF.
    assert [a for _, a in tb.quality_actions([720], 12)] == ["best", "h480", "mp3", "gif"]
    assert tb.quality_actions([])[-1] == ("🎞 GIF", "gif")
    assert tb.video_format_selector(720) == "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"
