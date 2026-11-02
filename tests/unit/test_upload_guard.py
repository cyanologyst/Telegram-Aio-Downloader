"""Uploads refuse files that are still being downloaded."""

import os
import time

import pytest

import app.bot.telegram_bot as tb
from app.services import upload_guard, user_settings
from tests.fakes import make_context


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "Download"
    root.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", root)
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "upload_jobs", {})
    monkeypatch.setattr(tb, "upload_refusal", REAL_REFUSAL)  # conftest turns it off
    return root


def _aria2(*files):
    return [{"gid": "g1", "status": "active", "files": list(files)}]


def _old(path, age=600):
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


async def _no_sleep(_):
    return None


async def _unreadable(_):
    return False


def _statuses(value):
    async def fetch():
        return value

    return fetch


# ------------------------------------------------------------------ aria2


async def test_file_of_an_active_aria2_download_is_refused(tmp_path):
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"x" * 100)
    _old(movie)
    statuses = _aria2({"path": str(movie), "length": "1000", "completedLength": "425"})

    reason = await upload_guard.check_upload(
        movie, fetch_statuses=_statuses(statuses), probe_readable=_unreadable, sleep=_no_sleep
    )

    assert reason == "Refusing: file still downloading (42.5% complete), try after completion"


async def test_finished_file_of_a_running_torrent_is_allowed(tmp_path):
    done = tmp_path / "ep1.mkv"
    done.write_bytes(b"x")
    _old(done)
    statuses = _aria2(
        {"path": str(done), "length": "1000", "completedLength": "1000"},
        {"path": str(tmp_path / "ep2.mkv"), "length": "1000", "completedLength": "3"},
    )

    async def readable(_):
        return True

    assert (
        await upload_guard.check_upload(
            done, fetch_statuses=_statuses(statuses), probe_readable=readable, sleep=_no_sleep
        )
        is None
    )


async def test_aria2_down_falls_back_to_its_control_file(tmp_path):
    movie = tmp_path / "movie.mp4"
    movie.write_bytes(b"x")
    (tmp_path / "movie.mp4.aria2").write_bytes(b"ctl")

    async def unreachable():
        raise tb.Aria2ConnectionError("refused")

    reason = await upload_guard.check_upload(
        movie, fetch_statuses=unreachable, probe_readable=_unreadable, sleep=_no_sleep
    )
    assert reason and "still being written" in reason


# ---------------------------------------------------------------- ffprobe


async def test_stable_but_unreadable_video_is_allowed(tmp_path):
    movie = tmp_path / "broken.mp4"
    movie.write_bytes(b"no moov here")
    _old(movie)

    assert (
        await upload_guard.check_upload(
            movie, fetch_statuses=_statuses([]), probe_readable=_unreadable, sleep=_no_sleep
        )
        is None
    )


async def test_unreadable_video_whose_size_changes_is_refused(tmp_path):
    movie = tmp_path / "growing.mp4"
    movie.write_bytes(b"x" * 10)
    _old(movie)

    async def grow(_):
        with movie.open("ab") as fh:
            fh.write(b"more")
        _old(movie, age=500)

    reason = await upload_guard.check_upload(
        movie, fetch_statuses=_statuses([]), probe_readable=_unreadable, sleep=grow
    )
    assert reason == "Refusing: file still downloading (still being written), try after completion"


async def test_unreadable_video_written_moments_ago_is_refused(tmp_path):
    movie = tmp_path / "fresh.mp4"
    movie.write_bytes(b"x")

    reason = await upload_guard.check_upload(
        movie, fetch_statuses=_statuses([]), probe_readable=_unreadable, sleep=_no_sleep
    )
    assert reason and "still being written" in reason


async def test_non_videos_skip_the_ffprobe_check(tmp_path):
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"x")  # brand new, but not a video

    async def must_not_probe(_):
        raise AssertionError("probed a non-video")

    assert (
        await upload_guard.check_upload(
            notes, fetch_statuses=_statuses([]), probe_readable=must_not_probe, sleep=_no_sleep
        )
        is None
    )


# ------------------------------------------------------------- upload flow


async def test_upload_flow_refuses_and_keeps_a_downloading_file(monkeypatch):
    movie = tb.DOWNLOAD_DIR / "movie.mkv"
    movie.write_bytes(b"x" * 10)
    ok = tb.DOWNLOAD_DIR / "ok.bin"
    ok.write_bytes(b"x" * 10)
    _old(movie)
    sent = []

    async def downloading():
        return _aria2({"path": str(movie), "length": "100", "completedLength": "10"})

    async def fake_send(rel_path, progress_callback=None, chat_id="me", **_):
        sent.append(rel_path)

    async def logged_in():
        return object()

    monkeypatch.setattr(tb, "unfinished_aria2_downloads", downloading)
    monkeypatch.setattr(tb, "pyrogram_send_file", fake_send)
    monkeypatch.setattr(tb, "get_pyrogram_client", logged_in)
    await user_settings.update_setting(5, "auto_delete_files_after_upload", True)
    context = make_context()

    job = await tb.upload_files_via_pyrogram(
        context.application, 1, 2, ["movie.mkv", "ok.bin"], title="t", user_id=5
    )

    assert [os.path.basename(p) for p in sent] == ["ok.bin"]
    assert movie.exists()  # not deleted by auto-delete
    assert job["sent_count"] == 1
    text = context.bot.called("edit_message_text")[-1].kwargs["text"]
    assert (
        "movie.mkv: Refusing: file still downloading (10.0% complete), try after completion" in text
    )


async def test_upload_flow_all_refused_says_so(monkeypatch):
    movie = tb.DOWNLOAD_DIR / "movie.mkv"
    movie.write_bytes(b"x")

    async def downloading():
        return _aria2({"path": str(movie), "length": "4", "completedLength": "1"})

    async def never_send(*_a, **_k):
        raise AssertionError("sent an unfinished file")

    monkeypatch.setattr(tb, "unfinished_aria2_downloads", downloading)
    monkeypatch.setattr(tb, "pyrogram_send_file", never_send)
    context = make_context()

    job = await tb.upload_files_via_pyrogram(
        context.application, 1, 2, ["movie.mkv"], title="t", user_id=5
    )

    assert job["status"] == "refused"
    text = context.bot.called("edit_message_text")[-1].kwargs["text"]
    assert "Upload refused" in text and "25.0% complete" in text


async def test_unfinished_downloads_never_starts_aria2(monkeypatch):
    calls = []

    async def call(method, *params):
        calls.append(method)
        return [{"gid": method}]

    async def must_not_start():
        raise AssertionError("tried to start aria2")

    monkeypatch.setattr(tb.aria2_client, "call", call)
    monkeypatch.setattr(tb.aria2_client, "ensure_started", must_not_start)

    statuses = await REAL_UNFINISHED()  # conftest stubs tb's copy out
    assert calls == ["aria2.tellActive", "aria2.tellWaiting"] and len(statuses) == 2


REAL_UNFINISHED = tb.unfinished_aria2_downloads  # captured at import, before any stub
REAL_REFUSAL = tb.upload_refusal
