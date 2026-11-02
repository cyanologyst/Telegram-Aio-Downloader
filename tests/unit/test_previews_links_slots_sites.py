"""Preview toggle and pairing, multi-link messages, download slots, the Supported sites list."""

import asyncio

import pytest

import app.bot.telegram_bot as tb
from app.bot.views import home as home_views
from app.bot.views import jobs as job_views
from app.bot.views import settings as settings_views
from app.services import supported_sites, user_settings
from app.services.download_slots import DownloadSlots, limit_of
from app.services.link_batch import split_links
from tests.fakes import callback_update, make_context, text_update


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "Download"
    root.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", root)
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "upload_jobs", {})
    monkeypatch.setattr(tb, "link_requests", {})
    return root


def _data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ---------------------------------------------------------------- preview toggle


def test_preview_toggle_defaults_on_and_is_in_settings():
    s = user_settings.get_user_settings(1)
    assert s["send_previews_after_upload"] is True
    text, markup = settings_views.settings_screen(s)
    assert "Preview after each uploaded video: On" in text
    assert "set:send_previews_after_upload" in _data(markup)
    assert tb.wants_upload_previews(1, "a/movie.mp4")
    assert not tb.wants_upload_previews(1, "a/notes.txt")


async def test_preview_toggle_turns_off():
    context = make_context()
    await tb.on_button(callback_update(context, "set:send_previews_after_upload"), context)
    assert user_settings.get_user_settings(1)["send_previews_after_upload"] is False
    assert not tb.wants_upload_previews(1, "movie.mp4")


# ---------------------------------------------------------------- video -> preview pairing


async def test_concurrent_auto_uploads_keep_each_preview_next_to_its_video(monkeypatch):
    sent = []

    async def upload_one(app, upload_id, rel, label):
        sent.append(("video", rel))
        await asyncio.sleep(0.01)
        return True

    async def previews(app, chat_id, target, rel, *, delete_after, prune=True):
        await asyncio.sleep(0.02)  # slower than an upload: the other job would cut in
        sent.append(("preview", rel))

    monkeypatch.setattr(tb, "_upload_one_file", upload_one)
    monkeypatch.setattr(tb, "send_upload_previews", previews)
    context = make_context()
    jobs = [
        tb.upload_files_via_pyrogram(
            context.application,
            1,
            100 + n,
            [f"job{n}/v{i}.mp4" for i in range(3)],
            title=f"Job {n}",
            user_id=1,
            auto=True,
        )
        for n in range(3)
    ]
    await asyncio.gather(*jobs)

    assert len(sent) == 18
    for i in range(0, len(sent), 2):
        assert sent[i][0] == "video" and sent[i + 1] == ("preview", sent[i][1])


async def test_no_preview_when_turned_off(monkeypatch):
    await user_settings.update_setting(1, "send_previews_after_upload", False)
    previews = []

    async def upload_one(app, upload_id, rel, label):
        return True

    async def record(*args, **kwargs):
        previews.append(args)

    monkeypatch.setattr(tb, "_upload_one_file", upload_one)
    monkeypatch.setattr(tb, "send_upload_previews", record)
    context = make_context()
    await tb.upload_files_via_pyrogram(
        context.application, 1, 5, ["v.mp4"], title="x", user_id=1, auto=True
    )
    assert previews == []


# ---------------------------------------------------------------- multi-link messages


def test_split_links_keeps_order_and_reports_bad_lines():
    batch = split_links(
        "here you go:\n"
        "https://youtu.be/a, https://youtu.be/b\n"
        "\n"
        "magnet:?xt=urn:btih:abc&dn=x\n"
        "https://youtu.be/a\n"
        "(https://example.com/file.zip)\n"
        "just text"
    )
    assert batch.links == [
        "https://youtu.be/a",
        "https://youtu.be/b",
        "magnet:?xt=urn:btih:abc&dn=x",
        "https://example.com/file.zip",
    ]
    assert batch.invalid == ["here you go:", "just text"]
    assert batch.duplicates == 1 and batch.is_batch


def test_one_link_is_not_a_batch():
    assert not split_links("https://youtu.be/a").is_batch
    assert not split_links("look\nhttps://youtu.be/a").is_batch


async def test_multi_link_message_queues_each_link_with_a_summary(monkeypatch):
    started = []

    def fake_start(kind):
        async def start(app, chat_id, link, *args, **kwargs):
            if "broken" in link:
                raise RuntimeError("tracker said no")
            job = {"id": len(started) + 1, "status": "starting", "kind": kind, "link": link}
            started.append(job)
            return job

        return start

    async def no_card(app, job, message=None):
        return None

    monkeypatch.setattr(tb, "start_aria2_download", fake_start("aria2"))
    monkeypatch.setattr(tb, "start_ytdlp_download", fake_start("yt-dlp"))
    monkeypatch.setattr(tb, "attach_job_card", no_card)
    monkeypatch.setattr(tb, "gallery_category", lambda url: None)
    await user_settings.update_setting(1, "max_concurrent_downloads", 2)
    context = make_context()
    text = (
        "magnet:?xt=urn:btih:abc\n"
        "not a link\n"
        "https://www.youtube.com/watch?v=x\n"
        "https://unknown.example/page\n"
        "magnet:?xt=urn:btih:broken\n"
        "https://example.com/big.zip"
    )

    await tb.on_text(text_update(context, text), context)

    assert [(j["kind"], j["link"]) for j in started] == [
        ("aria2", "magnet:?xt=urn:btih:abc"),
        ("yt-dlp", "https://www.youtube.com/watch?v=x"),
        ("aria2", "https://example.com/big.zip"),
    ]
    (summary,) = context.bot.called("reply_text")
    body = summary.kwargs["text"]
    assert "3 of 5 links queued" in body
    assert "Up to 2 downloads at once" in body
    assert "unknown.example" in body and "not a supported link" in body
    assert "tracker said no" in body
    assert "Lines without a link (1)" in body and "not a link" in body


# ---------------------------------------------------------------- download slots


def test_limit_is_clamped():
    assert limit_of({}) == 8
    assert limit_of({"max_concurrent_downloads": 0}) == 1
    assert limit_of({"max_concurrent_downloads": 99}) == 8
    assert limit_of({"max_concurrent_downloads": "x"}) == 8


async def test_slots_queue_in_order_and_report_positions():
    slots = DownloadSlots()
    slots.poll_seconds = 0.001
    limit = {"n": 1}
    positions = {2: [], 3: []}
    assert await slots.acquire(1, 1, lambda: limit["n"])
    second = asyncio.create_task(
        slots.acquire(1, 2, lambda: limit["n"], on_wait=positions[2].append)
    )
    third = asyncio.create_task(
        slots.acquire(1, 3, lambda: limit["n"], on_wait=positions[3].append)
    )
    await asyncio.sleep(0.02)
    assert not second.done() and not third.done()
    assert positions[2][-1] == 1 and positions[3][-1] == 2

    slots.release(1, 1)
    assert await second
    await asyncio.sleep(0.01)
    assert not third.done() and positions[3][-1] == 1

    limit["n"] = 2  # raising the limit in Settings lets it start
    assert await third
    assert slots.active(1) == 2


async def test_cancelled_while_queued_leaves_the_queue():
    slots = DownloadSlots()
    slots.poll_seconds = 0.001
    assert slots.try_acquire(1, 1, 1)
    cancelled = {"v": False}
    task = asyncio.create_task(slots.acquire(1, 2, lambda: 1, cancelled=lambda: cancelled["v"]))
    await asyncio.sleep(0.01)
    cancelled["v"] = True
    assert await task is False
    assert slots.position(1, 2) is None


def test_queued_card_shows_its_position():
    job = {"id": 7, "name": "x", "status": "queued", "queue_position": 3}
    assert job_views._state_line(job) == "Queued (position 3)"


async def test_concurrency_stepper(monkeypatch):
    assert _data(InlineRow(settings_views.concurrency_row(1)))[0] == "noop"
    assert _data(InlineRow(settings_views.concurrency_row(8)))[-1] == "noop"
    _, markup = settings_views.settings_screen(user_settings.get_user_settings(1))
    assert "mcd:7" in _data(markup)  # default 8: only ➖ steps

    context = make_context()
    await tb.on_button(callback_update(context, "mcd:3"), context)
    assert user_settings.get_user_settings(1)["max_concurrent_downloads"] == 3
    await tb.on_button(callback_update(context, "mcd:42"), context)
    assert user_settings.get_user_settings(1)["max_concurrent_downloads"] == 8


class InlineRow:
    def __init__(self, row):
        self.inline_keyboard = [row]


# ---------------------------------------------------------------- supported sites


def test_sites_list_is_paginated_in_the_bot():
    lines = supported_sites.site_lines()
    assert len(lines) > supported_sites.PER_PAGE
    text, markup = tb.build_sites_screen(0)
    data = _data(markup)
    assert "sites:1" in data and not any(d == "sites:-1" for d in data)
    assert "nav:home" in data
    assert all(b.url is None for row in markup.inline_keyboard for b in row)

    pages = supported_sites.page_count(len(lines))
    text, markup = tb.build_sites_screen(pages + 5)  # clamped to the last page
    assert f"page {pages}/{pages}" in text
    assert f"sites:{pages - 2}" in _data(markup) and f"sites:{pages}" not in _data(markup)


def test_sites_pages_hold_at_most_thirty_lines():
    lines = supported_sites.site_lines()
    for page in range(supported_sites.page_count(len(lines))):
        shown, _, _ = supported_sites.page_of(lines, page)
        assert 0 < len(shown) <= 30


def test_help_opens_the_in_bot_list():
    _, markup = home_views.help_screen("https://example.test")
    assert "sites:0" in _data(markup)
    assert all(b.url is None for row in markup.inline_keyboard for b in row)
