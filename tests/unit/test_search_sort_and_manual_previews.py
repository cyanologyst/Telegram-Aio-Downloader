"""Sortable torrent search results, and GIF previews after manual uploads."""

import asyncio
from pathlib import Path

import pytest

import app.bot.telegram_bot as tb
from app.bot.search import SearchUI
from app.services import user_settings
from app.services.torrent_search import (
    SearchResult,
    flip_sort_direction,
    next_sort_field,
    parse_sort,
    size_bytes,
    sort_items,
)
from tests.fakes import callback_update, make_context, text_update


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "Download"
    root.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", root)
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "upload_jobs", {})
    return root


ITEMS = [
    {"title": "beta", "size": "700 MB", "seeders": 50, "added": "2024-03-01"},
    {"title": "Alpha", "size": "1.5 GB", "seeders": 5, "added": "2025-01-10"},
    {"title": "gamma", "size": "2 KiB", "seeders": 900, "added": "2023-12-31 10:00"},
    {"title": "delta", "size": "?", "seeders": None, "added": ""},
]


def _order(spec):
    items = [dict(item) for item in ITEMS]
    sort_items(items, spec)
    return [item["title"] for item in items]


# ---------------------------------------------------------------- sort helpers


def test_size_parsing():
    assert size_bytes("1.5 GB") == int(1.5 * 1024**3)
    assert size_bytes("700 MiB") == 700 * 1024**2
    assert size_bytes("1,024 B") == 1024
    assert size_bytes("?") is None


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("seeders:desc", ["gamma", "beta", "Alpha", "delta"]),
        ("seeders:asc", ["Alpha", "beta", "gamma", "delta"]),
        ("size:desc", ["Alpha", "beta", "gamma", "delta"]),
        ("size:asc", ["gamma", "beta", "Alpha", "delta"]),
        ("date:desc", ["Alpha", "beta", "gamma", "delta"]),
        ("date:asc", ["gamma", "beta", "Alpha", "delta"]),
        ("name:asc", ["Alpha", "beta", "delta", "gamma"]),
        ("name:desc", ["gamma", "delta", "beta", "Alpha"]),
    ],
)
def test_sort_order_per_criterion_and_direction(spec, expected):
    # Results missing the value (delta) stay last in both directions.
    assert _order(spec) == expected


def test_sort_spec_cycling_and_flipping():
    assert parse_sort(None) == ("seeders", "desc")
    assert parse_sort("bogus:up") == ("seeders", "desc")
    assert parse_sort("name") == ("name", "asc")
    assert next_sort_field("seeders:asc") == "size:desc"
    assert next_sort_field("size:asc") == "date:desc"
    assert next_sort_field("date:desc") == "name:asc"
    assert next_sort_field("name:asc") == "seeders:desc"
    assert flip_sort_direction("size:desc") == "size:asc"
    assert flip_sort_direction("size:asc") == "size:desc"


# ---------------------------------------------------------------- search UI


class SortProvider:
    key = "tpb"
    label = "P-tpb"
    description = "test"
    paginates = False
    enabled = True
    categories = {"all": "🌐 All"}

    async def search_page(self, query, category, page):
        if page:
            return []
        return [
            SearchResult(
                title=item["title"], size=item["size"], seeders=item["seeders"],
                added=item["added"], raw={"id": item["title"]},
            )
            for item in ITEMS
        ]

    async def resolve(self, result, torrent_dir):
        raise AssertionError("not expected")


async def _search(context):
    ui = SearchUI([SortProvider()], None, None, Path("/tmp/unused"))
    await ui.open(context, text_update(context, "/tpb").message, "tpb", edit=False)
    await ui.handle_text(text_update(context, "q"), context)
    return ui


def _titles(context):
    return [item["title"] for item in context.user_data["search"]["items"]]


def _sort_buttons(context):
    markup = context.bot.calls[-1].kwargs["reply_markup"]
    return [
        (b.text, b.callback_data)
        for row in markup.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("srch:so:")
    ]


async def test_results_default_to_most_seeders_with_sort_buttons():
    context = make_context()
    await _search(context)
    assert _titles(context) == ["gamma", "beta", "Alpha", "delta"]
    assert _sort_buttons(context) == [
        ("Sort: 🟢 Seeders", "srch:so:1:size:desc"),
        ("↓ Most", "srch:so:1:seeders:asc"),
    ]
    assert "🟢 Seeders ↓" in context.bot.calls[-1].kwargs["text"]


async def test_sort_buttons_cycle_flip_rerender_and_persist():
    context = make_context()
    ui = await _search(context)

    await ui.on_callback(callback_update(context, "srch:so:1:size:desc"), context)
    assert _titles(context) == ["Alpha", "beta", "gamma", "delta"]
    assert _sort_buttons(context) == [
        ("Sort: 💾 Size", "srch:so:1:date:desc"),
        ("↓ Largest", "srch:so:1:size:asc"),
    ]
    assert user_settings.get_user_settings(1)["search_sort"] == "size:desc"

    await ui.on_callback(callback_update(context, "srch:so:1:size:asc"), context)
    assert _titles(context) == ["gamma", "beta", "Alpha", "delta"]
    assert user_settings.get_user_settings(1)["search_sort"] == "size:asc"

    # The next search starts with the saved choice.
    context2 = make_context()
    await _search(context2)
    assert _titles(context2) == ["gamma", "beta", "Alpha", "delta"]
    assert ("↑ Smallest", "srch:so:1:size:desc") in _sort_buttons(context2)


async def test_sort_resets_to_first_page():
    context = make_context()
    ui = await _search(context)
    context.user_data["search"]["page"] = 3
    await ui.on_callback(callback_update(context, "srch:so:1:name:asc"), context)
    assert context.user_data["search"]["page"] == 0
    assert _titles(context) == ["Alpha", "beta", "delta", "gamma"]


# ---------------------------------------------------------------- manual-upload previews


def _fake_upload(sent):
    async def upload_one(app, upload_id, rel, label):
        sent.append(("video", rel, tb.upload_jobs[upload_id]["target"]))
        await asyncio.sleep(0.01)
        return True

    return upload_one


async def test_manual_upload_sends_preview_to_the_same_chat(monkeypatch):
    await user_settings.update_setting(1, "target_chat_id", -100123)
    sent, previews = [], []

    async def record(app, chat_id, target, rel, *, delete_after, prune=True):
        previews.append((chat_id, target, rel, delete_after, prune))
        sent.append(("preview", rel, target))

    monkeypatch.setattr(tb, "_upload_one_file", _fake_upload(sent))
    monkeypatch.setattr(tb, "send_upload_previews", record)
    context = make_context()
    await tb.send_single_file_via_pyrogram(context.application, 1, 5, "a/v.mp4", user_id=1)

    assert sent == [("video", "a/v.mp4", -100123), ("preview", "a/v.mp4", -100123)]
    # Manual: no folder pruning, and no delete unless the setting asks for it.
    assert previews == [(1, -100123, "a/v.mp4", False, False)]


async def test_manual_upload_to_saved_messages_also_gets_preview(monkeypatch):
    previews = []

    async def record(app, chat_id, target, rel, *, delete_after, prune=True):
        previews.append(target)

    monkeypatch.setattr(tb, "_upload_one_file", _fake_upload([]))
    monkeypatch.setattr(tb, "send_upload_previews", record)
    context = make_context()
    await tb.send_folder_files_via_pyrogram(
        context.application, 1, 5, "", file_list=["v.mkv", "notes.txt"], user_id=1
    )
    assert previews == ["me"]  # the video only, not the text file


async def test_manual_upload_no_preview_when_turned_off(monkeypatch):
    await user_settings.update_setting(1, "send_previews_after_upload", False)
    previews = []

    async def record(*args, **kwargs):
        previews.append(args)

    monkeypatch.setattr(tb, "_upload_one_file", _fake_upload([]))
    monkeypatch.setattr(tb, "send_upload_previews", record)
    context = make_context()
    await tb.send_single_file_via_pyrogram(context.application, 1, 5, "v.mp4", user_id=1)
    assert previews == []


async def test_manual_and_auto_uploads_keep_preview_adjacent(monkeypatch):
    await user_settings.update_setting(1, "target_chat_id", -100123)
    sent = []

    async def previews(app, chat_id, target, rel, *, delete_after, prune=True):
        await asyncio.sleep(0.02)  # slower than an upload: the other one would cut in
        sent.append(("preview", rel, target))

    monkeypatch.setattr(tb, "_upload_one_file", _fake_upload(sent))
    monkeypatch.setattr(tb, "send_upload_previews", previews)
    context = make_context()
    app = context.application
    await asyncio.gather(
        tb.send_folder_files_via_pyrogram(
            app, 1, 5, "", file_list=[f"m/v{i}.mp4" for i in range(3)], user_id=1
        ),
        tb.upload_files_via_pyrogram(
            app, 1, 6, [f"a/v{i}.mp4" for i in range(3)], title="auto", user_id=1, auto=True
        ),
    )
    assert len(sent) == 12
    for i in range(0, len(sent), 2):
        assert sent[i][0] == "video" and sent[i + 1] == ("preview", sent[i][1], sent[i][2])


async def test_manual_preview_delete_does_not_prune_folders(isolated, monkeypatch):
    await user_settings.update_setting(1, "auto_delete_files_after_upload", True)
    folder = isolated / "show"
    folder.mkdir()
    (folder / "v.mp4").write_bytes(b"x")

    async def no_previews(*args, **kwargs):
        return 0

    monkeypatch.setattr(tb, "send_gif_previews", no_previews)
    monkeypatch.setattr(tb, "_upload_one_file", _fake_upload([]))
    context = make_context()
    await tb.send_single_file_via_pyrogram(context.application, 1, 5, "show/v.mp4", user_id=1)
    assert not (folder / "v.mp4").exists()
    assert folder.is_dir()  # a manual upload leaves the (now empty) folder alone
