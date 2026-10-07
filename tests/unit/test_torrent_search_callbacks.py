"""Callback-data limits and result paging for the torrent search flows."""

import pytest

from app.downloaders.torrents.rarbg.handlers import PER_PAGE, RARBGHandlers
from app.downloaders.torrents.rarbg.keyboards import (
    rarbg_categories_keyboard,
    rarbg_header_keyboard,
)
from app.downloaders.torrents.tpb.keyboards import (
    tpb_categories_keyboard,
    tpb_header_keyboard,
)

# Telegram rejects callback_data longer than this, counted in bytes.
CALLBACK_DATA_LIMIT = 64

LONG_ASCII = "the lord of the rings the return of the king extended edition 2003"
PERSIAN = "بازی تاج و تخت فصل اول قسمت دوم با زیرنویس فارسی"


def all_callback_data(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


@pytest.mark.parametrize(
    "markup",
    [
        rarbg_categories_keyboard(),
        rarbg_header_keyboard("documentaries", 12, True),
        rarbg_header_keyboard("", 0, True),
        tpb_categories_keyboard(),
        tpb_header_keyboard("600", 99, True),
    ],
)
def test_callback_data_fits_telegram_limit(markup):
    for data in all_callback_data(markup):
        assert len(data.encode("utf-8")) <= CALLBACK_DATA_LIMIT, data


def test_keyboards_do_not_embed_the_query():
    """Long or non-ASCII queries used to overflow callback_data via the query."""
    for markup in (rarbg_categories_keyboard(), tpb_categories_keyboard()):
        for data in all_callback_data(markup):
            assert LONG_ASCII not in data
            assert PERSIAN not in data


def test_rarbg_page_callback_round_trips():
    markup = rarbg_header_keyboard("documentaries", 5, True)
    forward = [d for d in all_callback_data(markup) if d.startswith("rarbg_page_")]
    category, page = forward[-1].removeprefix("rarbg_page_").rsplit("_", 1)
    assert category == "documentaries"
    assert int(page) == 6


def test_tpb_page_callback_round_trips():
    markup = tpb_header_keyboard("600", 5, True)
    forward = [d for d in all_callback_data(markup) if d.startswith("tpb_page_")]
    category, page = forward[-1].removeprefix("tpb_page_").rsplit("_", 1)
    assert category == "600"
    assert int(page) == 6


# ---------------------------------------------------------------- paging


class _StubCrawler:
    """Returns 25 results per server page, like a real clone listing does."""

    PAGE_SIZE = 25

    def __init__(self, total_pages: int = 2):
        self.total_pages = total_pages
        self.requested_pages: list[int] = []

    async def search(self, query, category="", page=0):
        self.requested_pages.append(page)
        if page >= self.total_pages:
            return []
        base = page * self.PAGE_SIZE
        return [
            {"id": f"id-{base + i}", "name": f"result {base + i}"} for i in range(self.PAGE_SIZE)
        ]


class _Context:
    def __init__(self):
        self.user_data = {}


@pytest.mark.asyncio
async def test_pages_walk_the_whole_result_set():
    """Every fetched result must be reachable, not just the first PER_PAGE."""
    crawler = _StubCrawler(total_pages=1)
    handlers = RARBGHandlers(crawler, lambda uid, key: key)
    context = _Context()

    seen = []
    for page in range(_StubCrawler.PAGE_SIZE // PER_PAGE):
        window, _ = await handlers._results_for_page(context, "q", "", page)
        seen.extend(item["id"] for item in window)

    assert seen == [f"id-{i}" for i in range(_StubCrawler.PAGE_SIZE)]
    assert len(seen) == len(set(seen))


@pytest.mark.asyncio
async def test_paging_crosses_into_the_next_server_page():
    crawler = _StubCrawler(total_pages=2)
    handlers = RARBGHandlers(crawler, lambda uid, key: key)
    context = _Context()

    window, has_more = await handlers._results_for_page(context, "q", "", 5)

    assert [item["id"] for item in window] == [f"id-{i}" for i in range(25, 30)]
    assert has_more is True
    assert 1 in crawler.requested_pages


@pytest.mark.asyncio
async def test_has_more_is_false_at_the_end():
    crawler = _StubCrawler(total_pages=1)
    handlers = RARBGHandlers(crawler, lambda uid, key: key)
    context = _Context()

    window, has_more = await handlers._results_for_page(context, "q", "", 4)

    assert [item["id"] for item in window] == [f"id-{i}" for i in range(20, 25)]
    assert has_more is False


@pytest.mark.asyncio
async def test_changing_the_query_discards_the_cache():
    crawler = _StubCrawler(total_pages=2)
    handlers = RARBGHandlers(crawler, lambda uid, key: key)
    context = _Context()

    await handlers._results_for_page(context, "first", "", 0)
    await handlers._results_for_page(context, "second", "", 0)

    assert context.user_data["rarbg_cache"]["key"] == ["second", ""]
