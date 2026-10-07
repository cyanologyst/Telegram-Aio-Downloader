"""Jobs survive restarts: SQLite store and restore/reattach on startup."""

import asyncio

import pytest

import app.bot.telegram_bot as tb
from app.infrastructure.aria2_rpc import Aria2RpcError
from app.services import user_settings
from app.services.job_store import JobStore
from tests.fakes import make_context


def test_store_round_trip_drops_runtime_objects(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.sync(
        [
            {
                "id": 1,
                "status": "completed",
                "name": "a",
                "process": object(),
                "card": {"message_id": 5},
            },
            {"id": 2, "status": "downloading", "name": "b", "task": object()},
        ]
    )
    store.close()

    loaded = JobStore(tmp_path / "jobs.sqlite3").load()

    assert [j["id"] for j in loaded] == [1, 2]
    assert "process" not in loaded[0] and loaded[0]["card"] == {"message_id": 5}
    assert "task" not in loaded[1]


def test_sync_removes_cleared_jobs_and_load_keeps_recent_finished(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3", keep_finished=2)
    store.sync(
        [{"id": i, "status": "completed"} for i in range(1, 6)] + [{"id": 9, "status": "queued"}]
    )
    assert [j["id"] for j in store.load()] == [4, 5, 9]

    store.sync([{"id": 9, "status": "queued"}])
    assert [j["id"] for j in store.load()] == [9]


@pytest.fixture
def restart_env(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "status_messages", {})
    monkeypatch.setattr(tb, "job_counter", 0)
    store = JobStore(tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(tb, "job_store", store)
    monitored = []

    async def fake_monitor(app, job_id):
        monitored.append(job_id)

    monkeypatch.setattr(tb, "monitor_aria2_job", fake_monitor)

    class FakeAria2:
        known = {"gid-1"}
        pid = None
        added = []

        async def ensure_started(self):
            pass

        async def tell_status(self, gid):
            if gid in self.known:
                return {"gid": gid, "status": "active"}
            raise Aria2RpcError(f"GID {gid} is not found")

        async def tell_active(self):
            return []

        async def tell_waiting(self, *a):
            return []

        async def tell_stopped(self, *a):
            return []

        async def add_uri(self, uri, options):
            self.added.append(uri)
            return "gid-new"

    fake = FakeAria2()
    monkeypatch.setattr(tb, "aria2_client", fake)
    return store, monitored, fake


def _base(**job):
    return {"chat_id": 1, "user_id": 1, "name": "x", "started_at": 1, **job}


async def test_restore_resumes_aria2_and_flags_interrupted_jobs(restart_env):
    store, monitored, aria2 = restart_env
    store.sync(
        [
            _base(id=3, status="completed", card_final="completed"),
            _base(id=4, status="downloading", gid="gid-1", source="magnet:?xt=urn:btih:aa"),
            _base(id=5, status="downloading", gid="gid-gone", source="https://e.org/f.iso"),
            _base(id=6, status="downloading", provider="yt-dlp", url="https://youtu.be/x"),
        ]
    )
    context = make_context()

    await tb.restore_jobs(context.application)
    await asyncio.sleep(0)  # let the monitor tasks start
    while tb.background_tasks:
        await asyncio.gather(*list(tb.background_tasks), return_exceptions=True)
        await asyncio.sleep(0)  # let done-callbacks remove finished tasks

    assert tb.job_counter == 6
    assert sorted(monitored) == [4, 5]
    assert tb.download_jobs[4]["gid"] == "gid-1"
    assert tb.download_jobs[5]["gid"] == "gid-new" and aria2.added == ["https://e.org/f.iso"]
    assert tb.download_jobs[3]["status"] == "completed"

    interrupted = tb.download_jobs[6]
    assert interrupted["status"] == "failed" and "Tap Retry" in interrupted["last_line"]
    card = context.bot.called("send_message")[-1]
    assert card.kwargs["text"].startswith("❌")
    assert "job:retry:6" in [
        b.callback_data for row in card.kwargs["reply_markup"].inline_keyboard for b in row
    ]


async def test_new_jobs_continue_numbering_after_restore(restart_env, monkeypatch):
    store, _, _ = restart_env
    store.sync([_base(id=41, status="completed", card_final="completed")])
    context = make_context()

    await tb.restore_jobs(context.application)
    job = await tb.start_aria2_download(context.application, 1, "https://e.org/new.iso", 1)

    assert job["id"] == 42
    tb.download_jobs.clear()
