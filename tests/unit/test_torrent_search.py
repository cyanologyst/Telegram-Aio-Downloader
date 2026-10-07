"""Unified torrent search: providers, paging and the Telegram flow."""

from pathlib import Path

import pytest

from app.bot import search as search_mod
from app.bot.search import PER_PAGE, SearchUI
from app.services.torrent_search import (
    RARBGProvider,
    SearchResult,
    TPBProvider,
    fetch_until,
)
from tests.fakes import callback_update, make_context, text_update

CALLBACK_DATA_LIMIT = 64
PERSIAN = "بازی تاج و تخت فصل اول قسمت دوم با زیرنویس فارسی"


# ---------------------------------------------------------------- providers


class _StubRarbgCrawler:
    """25 results per server page, like a real clone listing."""

    base_url = "https://mirror.example"
    PAGE_SIZE = 25

    def __init__(self, total_pages=2):
        self.total_pages = total_pages
        self.requested_pages = []

    async def search(self, query, category="", page=0):
        self.requested_pages.append(page)
        if page >= self.total_pages:
            return []
        base = page * self.PAGE_SIZE
        # Same title on every row: rows must still be told apart by id.
        return [{"id": f"id-{base + i}", "name": "Same Name"} for i in range(self.PAGE_SIZE)]


def _session(provider_key="rarbg"):
    return {
        "sid": "1",
        "provider": provider_key,
        "query": "q",
        "category": "all",
        "items": [],
        "next_page": 0,
        "exhausted": False,
        "page": 0,
    }


async def test_rarbg_paging_reaches_every_result_and_crosses_server_pages():
    crawler = _StubRarbgCrawler(total_pages=2)
    session = _session()

    await fetch_until(RARBGProvider(crawler), session, 30)

    assert [item["raw"]["id"] for item in session["items"]] == [f"id-{i}" for i in range(50)]
    assert crawler.requested_pages == [0, 1]
    assert session["exhausted"] is False

    await fetch_until(RARBGProvider(crawler), session, 60)
    assert session["exhausted"] is True
    assert len(session["items"]) == 50


class _StubTpbCrawler:
    api_url = "https://apibay.example"

    async def search(self, query, category="0", page=0):
        return [
            {"id": "11", "name": "Ubuntu", "info_hash": "ABC", "size": "1024", "seeders": "5"},
            {"id": "0", "name": "No results returned", "info_hash": "0" * 40},
        ]


async def test_tpb_drops_placeholder_rows_and_builds_magnets():
    results = await TPBProvider(_StubTpbCrawler()).search_page("ubuntu", "all", 0)

    assert [r.title for r in results] == ["Ubuntu"]
    assert results[0].magnet.startswith("magnet:?xt=urn:btih:ABC")
    assert results[0].seeders == 5
    assert await TPBProvider(_StubTpbCrawler()).search_page("ubuntu", "all", 1) == []


# ---------------------------------------------------------------- UI flow


class FakeProvider:
    paginates = False
    description = "test index"

    def __init__(self, key, count=8, enabled=True):
        self.key = key
        self.label = f"P-{key}"
        self.categories = {"all": "🌐 All", "movies": "🎬 Movies"}
        self.count = count
        self._enabled = enabled
        self.queries = []

    @property
    def enabled(self):
        return self._enabled

    async def search_page(self, query, category, page):
        self.queries.append((query, category, page))
        if page > 0:
            return []
        return [
            SearchResult(
                title=f"{query} <{i}>",
                size="1 GB",
                seeders=i,
                magnet=f"magnet:?xt=urn:btih:{i}",
                raw={"id": str(i)},
            )
            for i in range(self.count)
        ]

    async def resolve(self, result, torrent_dir):
        from app.services.torrent_search import ResolvedSource

        return ResolvedSource(source=result["magnet"])


class Recorder:
    def __init__(self, duplicate=False):
        self.calls = []
        self.duplicate = duplicate

    async def __call__(self, update, context, source, force):
        self.calls.append((source, force))
        if self.duplicate and not force:
            return {"id": 0, "name": "dup.mkv", "status": "duplicate"}
        return {"id": 7, "status": "queued"}


async def _no_select(update, context, path, title):
    raise AssertionError("not expected")


def _ui(providers, recorder=None):
    return SearchUI(providers, recorder or Recorder(), _no_select, Path("/tmp/unused"))


def _markup(call):
    return call.kwargs.get("reply_markup")


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


async def test_picker_then_prompt_then_results():
    tpb, rarbg = FakeProvider("tpb"), FakeProvider("rarbg")
    ui = _ui([tpb, rarbg, FakeProvider("prowlarr", enabled=False)])
    context = make_context()

    await ui.open(context, text_update(context, "/search").message, edit=False)
    picker = context.bot.calls[-1]
    labels = [b.text for b in _buttons(_markup(picker))]
    assert labels == ["P-tpb", "P-rarbg", "✖ Close"]  # unconfigured sources hidden
    assert picker.kwargs["parse_mode"] == "HTML"

    update = callback_update(context, "srch:p:tpb")
    await ui.on_callback(update, context)
    assert "Send what you're looking for" in context.bot.texts()[-1]
    assert len(update.callback_query.answers) == 1

    consumed = await ui.handle_text(text_update(context, "ubuntu & <friends>"), context)

    assert consumed is True
    results = context.bot.calls[-1]
    assert "&lt;friends&gt;" in results.kwargs["text"]  # escaped for HTML
    assert f"Results 1–{PER_PAGE} of 8" in results.kwargs["text"]
    numbers = [b.text for b in _buttons(_markup(results)) if b.text.isdigit()]
    assert numbers == [str(i) for i in range(1, PER_PAGE + 1)]
    assert tpb.queries == [("ubuntu & <friends>", "all", 0)]


async def test_single_source_skips_the_picker():
    ui = _ui([FakeProvider("tpb")])
    context = make_context()

    await ui.open(context, text_update(context, "/search").message, edit=False)

    assert "Send what you're looking for" in context.bot.texts()[-1]
    assert ui.is_waiting(context)


async def test_waiting_state_expires(monkeypatch):
    ui = _ui([FakeProvider("tpb")])
    context = make_context()
    await ui.open(context, text_update(context, "/tpb").message, "tpb", edit=False)

    later = search_mod.time.time() + search_mod.WAIT_SECONDS + 1
    monkeypatch.setattr(search_mod.time, "time", lambda: later)

    assert await ui.handle_text(text_update(context, "help"), context) is False


async def _searched(ui, context, query="ubuntu"):
    await ui.open(context, text_update(context, "/tpb").message, "tpb", edit=False)
    await ui.handle_text(text_update(context, query), context)


async def test_callback_data_fits_telegram_limit_for_any_query():
    ui = _ui([FakeProvider("tpb", count=20)])
    context = make_context()
    await _searched(ui, context, PERSIAN * 3)

    await ui.on_callback(callback_update(context, "srch:cat:1"), context)
    await ui.on_callback(callback_update(context, "srch:o:1:0"), context)

    for call in context.bot.calls:
        markup = _markup(call)
        if markup:
            for button in _buttons(markup):
                assert len(button.callback_data.encode()) <= CALLBACK_DATA_LIMIT


async def test_detail_download_and_duplicate_override():
    recorder = Recorder(duplicate=True)
    ui = _ui([FakeProvider("tpb")], recorder)
    context = make_context()
    await _searched(ui, context)

    await ui.on_callback(callback_update(context, "srch:o:1:2"), context)
    detail = context.bot.calls[-1]
    assert "ubuntu &lt;2&gt;" in detail.kwargs["text"]
    assert [b.text for b in _buttons(_markup(detail))] == [
        "📥 Download",
        "🧲 Magnet link",
        "⬅ Back to results",
    ]

    first = callback_update(context, "srch:dl:1:2")
    await ui.on_callback(first, context)
    assert "already in your downloads" in context.bot.texts()[-1]
    assert len(first.callback_query.answers) == 1

    await ui.on_callback(callback_update(context, "srch:dl:1:2:f"), context)
    assert recorder.calls == [("magnet:?xt=urn:btih:2", False), ("magnet:?xt=urn:btih:2", True)]
    assert "Download started" in context.bot.texts()[-1] and "Job #7" in context.bot.texts()[-1]


async def test_buttons_from_an_older_search_say_so():
    ui = _ui([FakeProvider("tpb")])
    context = make_context()
    await _searched(ui, context, "first")
    await _searched(ui, context, "second")

    stale = callback_update(context, "srch:dl:1:0")
    await ui.on_callback(stale, context)

    assert stale.callback_query.answers[0]["show_alert"] is True
    assert "older search" in stale.callback_query.answers[0]["text"]


async def test_category_change_searches_again():
    provider = FakeProvider("tpb")
    ui = _ui([provider])
    context = make_context()
    await _searched(ui, context)

    await ui.on_callback(callback_update(context, "srch:c:1:movies"), context)

    assert provider.queries[-1] == ("ubuntu", "movies", 0)
    assert "🎬 Movies" in context.bot.texts()[-1]


async def test_next_page_and_close():
    ui = _ui([FakeProvider("tpb", count=8)])
    context = make_context()
    await _searched(ui, context)

    await ui.on_callback(callback_update(context, "srch:pg:1:1"), context)
    assert "Results 7–8 of 8" in context.bot.texts()[-1]

    await ui.on_callback(callback_update(context, "srch:x"), context)
    assert context.bot.texts()[-1] == "🔍 Search closed."


@pytest.mark.parametrize("text", ["magnet:?xt=urn:btih:abc", "https://youtu.be/x"])
async def test_links_cancel_a_pending_search(text, monkeypatch):
    import app.bot.telegram_bot as tb

    ui = _ui([FakeProvider("tpb")])
    monkeypatch.setattr(tb, "search_ui", ui)
    context = make_context()
    await ui.open(context, text_update(context, "/tpb").message, "tpb", edit=False)

    async def fake_start(*args, **kwargs):
        return {"id": 1, "name": "x", "gid": "g", "source_type": "magnet"}

    monkeypatch.setattr(tb, "start_aria2_download", fake_start)
    monkeypatch.setattr(tb, "is_duplicate_name", lambda name: False)

    async def no_status(*args, **kwargs):
        return None

    monkeypatch.setattr(tb, "update_status_message", no_status)
    await tb.on_text(text_update(context, text), context)

    assert not ui.is_waiting(context)
