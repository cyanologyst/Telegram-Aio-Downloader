"""Regressions found in the final code review of the overhaul."""

import asyncio
from pathlib import Path

import pytest

import app.bot.telegram_bot as tb
from app.bot.search import SearchUI
from app.bot.views import home as home_views
from app.bot.views import jobs as job_views
from app.services import user_settings
from app.services.torrent_search import ProwlarrProvider, SearchResult, fetch_until
from tests.fakes import callback_update, make_context, text_update


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "Download"
    root.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", root)
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "status_messages", {})
    return root


def test_organize_at_root_never_touches_the_gallery_library(isolated):
    (isolated / "Gallery" / "reddit").mkdir(parents=True)
    (isolated / "Gallery" / "reddit" / "clip.mp4").write_bytes(b"v")

    plan = tb.build_organize_plan("", tb.active_job_names())

    assert plan.moves == []


def test_batch_jobs_have_no_retry_button():
    job = {"id": 1, "name": "x", "status": "failed", "provider": "pornhub-model"}
    _, markup = job_views.job_card(job)
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == ["job:dismiss:1"]


async def test_retry_errors_are_shown(monkeypatch):
    tb.download_jobs[2] = {
        "id": 2,
        "name": "x",
        "status": "failed",
        "provider": "yt-dlp",
        "url": "u",
        "chat_id": 1,
        "user_id": 1,
    }

    async def broken(*args, **kwargs):
        raise RuntimeError("yt-dlp missing")

    monkeypatch.setattr(tb, "start_ytdlp_download", broken)
    context = make_context()
    update = callback_update(context, "job:retry:2")

    await tb.handle_job_callback(update, context)

    answer = update.callback_query.answers[0]
    assert answer["show_alert"] and "yt-dlp missing" in answer["text"]


async def test_cancelling_a_gallery_job_before_it_starts_stops_the_task():
    started = asyncio.Event()

    async def never_finishes():
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(never_finishes())
    await started.wait()
    tb.download_jobs[3] = {
        "id": 3,
        "name": "g",
        "status": "starting",
        "provider": "gallery-dl",
        "process": None,
        "task": task,
    }

    ok, _ = await tb.cancel_job(3)
    await asyncio.sleep(0)

    assert ok and task.cancelled()
    assert tb.download_jobs[3]["status"] == "cancelled"


async def test_keyboard_buttons_are_not_search_queries(monkeypatch):
    class Provider:
        key, label, description = "tpb", "TPB", "x"
        categories = {"all": "All"}
        enabled = True
        paginates = False
        queries = []

        async def search_page(self, query, category, page):
            self.queries.append(query)
            return []

    provider = Provider()
    ui = SearchUI([provider], None, None, Path("/tmp"))
    monkeypatch.setattr(tb, "search_ui", ui)
    context = make_context()
    await ui.open(context, text_update(context, "/tpb").message, "tpb", edit=False)

    await tb.on_text(text_update(context, home_views.KEY_STATUS), context)

    assert provider.queries == []
    assert not ui.is_waiting(context)
    assert "📊 <b>Status</b>" in context.bot.texts()[-1]


async def test_prowlarr_releases_with_the_same_title_stay_separate():
    class Client:
        enabled = True

        async def search(self, query, category):
            return [
                {
                    "token": str(i),
                    "title": "Same.Release.1080p",
                    "size": 1,
                    "seeders": i,
                    "indexer": f"idx{i}",
                    "magnet_url": "",
                    "download_url": f"http://x/{i}",
                }
                for i in range(3)
            ]

    session = {
        "sid": "1",
        "provider": "prowlarr",
        "query": "q",
        "category": "all",
        "items": [],
        "next_page": 0,
        "exhausted": False,
        "page": 0,
    }

    await fetch_until(ProwlarrProvider(Client()), session, 10)

    assert [item["source"] for item in session["items"]] == ["idx0", "idx1", "idx2"]


async def test_category_change_during_a_slow_fetch_does_not_mix_results():
    release = asyncio.Event()

    class Provider:
        key, label, description = "rarbg", "RARBG", "x"
        categories = {"all": "All", "movies": "Movies"}
        enabled = True
        paginates = True

        async def search_page(self, query, category, page):
            if category == "all" and page > 0:
                await release.wait()  # the slow page
            if page > 1:
                return []
            return [
                SearchResult(title=f"{category}-{page}-{i}", raw={"id": f"{category}-{page}-{i}"})
                for i in range(7)
            ]

    ui = SearchUI([Provider()], None, None, Path("/tmp"))
    context = make_context()
    await ui.open(context, text_update(context, "/rarbg").message, "rarbg", edit=False)
    await ui.handle_text(text_update(context, "q"), context)

    slow = asyncio.create_task(ui.on_callback(callback_update(context, "srch:pg:1:1"), context))
    await asyncio.sleep(0.05)
    await ui.on_callback(callback_update(context, "srch:c:1:movies"), context)
    release.set()
    await slow

    titles = [item["title"] for item in context.user_data["search"]["items"]]
    assert titles and all(title.startswith("movies-") for title in titles)
    assert "Movies" in context.bot.texts()[-1]
