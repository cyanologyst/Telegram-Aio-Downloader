"""File browser, selection mode and archive flows."""

import asyncio

import pytest

import app.bot.telegram_bot as tb
from app.bot.views import files as file_views
from app.services import user_settings
from tests.fakes import callback_update, make_context, text_update


@pytest.fixture(autouse=True)
def download_dir(tmp_path, monkeypatch):
    root = tmp_path / "Download"
    root.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", root)
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "batch_select_sessions", {})
    monkeypatch.setattr(tb, "pending_zip_name_sessions", {})
    monkeypatch.setattr(tb, "search_ui", None)
    return root


def _write(root, rel, data=b"x"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _markup(call):
    return call.kwargs.get("reply_markup")


def _buttons(call):
    markup = _markup(call)
    return [b for row in markup.inline_keyboard for b in row] if markup else []


def _button(call, text_part):
    return next(b for b in _buttons(call) if text_part in b.text)


async def _drain():
    while tb.background_tasks:
        await asyncio.gather(*list(tb.background_tasks), return_exceptions=True)
        await asyncio.sleep(0)  # let done-callbacks remove finished tasks


# ---------------------------------------------------------------- views


def _labels(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


def test_every_item_is_a_button_with_a_tidy_name():
    entries = [
        {"name": "Show", "rel_path": "Show", "is_dir": True, "size": 0, "count": 3},
        {
            "name": "میـوزاده - https:⧸⧸t.co⧸64Y2 [2107907522107494400].mp4",
            "rel_path": "x.mp4",
            "is_dir": False,
            "size": 2048,
        },
    ]

    text, markup = file_views.browser_screen("", entries, 0, lambda p: p, free_bytes=10 * 1024**3)

    assert text.startswith("📁 <b>Files</b> · 💽 10.0 GB free")
    labels = _labels(markup)
    assert labels[0] == "📁 \u2068Show\u2069 · 3 items"
    assert labels[1] == "🎬 \u2068میـوزاده.mp4\u2069 · 2.0 KB"  # link and id dropped
    assert "⬆ Up" not in labels  # already at the root
    assert {"🔍 Find", "☑️ Select", "⇅ 🕘 Newest"} <= set(labels)


def test_recent_downloads_come_first_at_the_root():
    recent = [{"name": "song.mp3", "rel_path": "Spotify/song.mp3", "is_dir": False, "size": 5}]
    entries = [{"name": "Spotify", "rel_path": "Spotify", "is_dir": True, "size": 0, "count": 1}]

    _, markup = file_views.browser_screen("", entries, 0, lambda p: p, recent=recent)

    labels = _labels(markup)
    assert labels[:4] == [
        "🕘 Recent downloads",
        "🎵 \u2068song.mp3\u2069 · 5 B",
        "📂 All files",
        "📁 \u2068Spotify\u2069 · 1 items",
    ]


def test_browser_pages_and_empty_folder():
    entries = [
        {"name": f"f{i}", "rel_path": f"f{i}", "is_dir": False, "size": 1} for i in range(20)
    ]
    text, markup = file_views.browser_screen("x", entries, 2, lambda p: p)
    assert "page 3/3" in text and "◀ Prev" in _labels(markup)
    assert "📁 <b>Files › x</b>" in text
    empty_text, _ = file_views.browser_screen("x", [], 0, lambda p: p)
    assert "This folder is empty." in empty_text


def test_sorting():
    entries = [
        {"name": "b", "rel_path": "b", "is_dir": False, "size": 9, "mtime": 1},
        {"name": "a", "rel_path": "a", "is_dir": False, "size": 1, "mtime": 3},
        {"name": "z", "rel_path": "z", "is_dir": True, "size": 0, "mtime": 0},
    ]
    names = lambda order: [e["name"] for e in file_views.sort_entries(entries, order)]  # noqa: E731
    assert names("new") == ["z", "a", "b"]
    assert names("name") == ["z", "a", "b"]
    assert names("size") == ["z", "b", "a"]
    assert file_views.next_sort("new") == "name"


# ---------------------------------------------------------------- browsing


async def test_open_folder_then_file_then_back(download_dir):
    _write(download_dir, "Movies/clip.mp4", b"v" * 10)
    context = make_context()

    await tb.files_cmd(text_update(context, "/files"), context)
    root = context.bot.calls[-1]
    assert "📁 \u2068Movies\u2069 · 1 items" in [b.text for b in _buttons(root)]

    await tb.on_button(callback_update(context, _button(root, "Movies").callback_data), context)
    folder = context.bot.calls[-1]
    assert "📁 <b>Files › Movies</b>" in folder.kwargs["text"]

    await tb.on_button(callback_update(context, _button(folder, "clip.mp4").callback_data), context)
    detail = context.bot.calls[-1]
    assert "🎬 <b>clip.mp4</b>" in detail.kwargs["text"]
    labels = [b.text for b in _buttons(detail)]
    assert {"📨 Send here", "🗜 Zip", "✏️ Rename", "🗑 Delete", "🎬 Convert"} <= set(labels)
    assert "📤 Saved Messages" not in labels  # no Pyrogram login in tests

    await tb.on_button(callback_update(context, _button(detail, "Back").callback_data), context)
    assert "📁 <b>Files › Movies</b>" in context.bot.texts()[-1]


async def test_internal_files_and_empty_folders_are_hidden(download_dir):
    _write(download_dir, ".aria2.rpc-secret", b"secret")
    _write(download_dir, "_torrents/x.torrent")
    (download_dir / "Adult").mkdir()
    _write(download_dir, "Spotify/song.mp3")
    context = make_context()

    await tb.files_cmd(text_update(context, "/files"), context)
    labels = " ".join(b.text for b in _buttons(context.bot.calls[-1]))

    assert "Spotify" in labels and "song.mp3" in labels  # folder, and under Recent
    assert "rpc-secret" not in labels and "_torrents" not in labels and "Adult" not in labels
    assert tb.get_all_files_in_folder("") == ["Spotify/song.mp3"]


async def test_send_here_sends_by_type(download_dir):
    _write(download_dir, "song.mp3")
    _write(download_dir, "notes.txt")
    context = make_context()

    for name in ("song.mp3", "notes.txt"):
        await tb.send_file_here(context.application, 1, name)

    assert [c.method for c in context.bot.calls] == ["send_audio", "send_document"]


async def test_rename_keeps_the_extension(download_dir):
    _write(download_dir, "Movies/clip [abc123xyz].mp4")
    context = make_context()
    token = tb.encode_path("Movies/clip [abc123xyz].mp4")

    await tb.on_button(callback_update(context, f"fb:ren:0:{token}"), context)
    assert "Send the new name" in context.bot.texts()[-1]

    await tb.on_text(text_update(context, "a/b"), context)
    assert "no slashes" in context.bot.texts()[-1]

    await tb.on_text(text_update(context, "Holiday"), context)
    assert (download_dir / "Movies/Holiday.mp4").exists()
    assert "🎬 <b>Holiday.mp4</b>" in context.bot.texts()[-1]
    assert "files_wait" not in context.user_data


async def test_find_searches_every_folder(download_dir):
    _write(download_dir, "Gallery/reddit/cat.gif")
    _write(download_dir, "Spotify/Cat Stevens - Wild World.mp3")
    _write(download_dir, "dog.jpg")
    context = make_context()

    await tb.on_button(callback_update(context, "fb:find:0:"), context)
    await tb.on_text(text_update(context, "CAT"), context)

    result = context.bot.calls[-1]
    assert "2 matches" in result.kwargs["text"]
    labels = [b.text for b in _buttons(result)]
    assert any("cat.gif" in label for label in labels)
    assert not any("dog" in label for label in labels)


async def test_a_link_cancels_a_pending_rename(download_dir, monkeypatch):
    _write(download_dir, "a.txt")
    context = make_context()
    await tb.on_button(callback_update(context, f"fb:ren:0:{tb.encode_path('a.txt')}"), context)

    started = []

    async def fake_aria2(app, chat_id, source, user_id=None):
        started.append(source)
        return {"id": 1, "name": "x", "status": "queued", "chat_id": chat_id}

    async def no_card(*args, **kwargs):
        return None

    monkeypatch.setattr(tb, "start_aria2_download", fake_aria2)
    monkeypatch.setattr(tb, "attach_job_card", no_card)
    await tb.on_text(text_update(context, "magnet:?xt=urn:btih:abc&dn=x"), context)

    assert started and (download_dir / "a.txt").exists()
    assert "files_wait" not in context.user_data


async def test_delete_file_asks_then_returns_to_the_folder(download_dir):
    path = _write(download_dir, "Movies/clip.mp4")
    context = make_context()
    token = tb.encode_path("Movies/clip.mp4")

    await tb.on_button(callback_update(context, f"fb:delete_confirm:0:{token}"), context)
    confirm = context.bot.calls[-1]
    assert "Permanently delete" in confirm.kwargs["text"] and path.exists()

    update = callback_update(context, _button(confirm, "Yes, delete").callback_data)
    await tb.on_button(update, context)

    assert not path.exists()
    assert update.callback_query.answers[0]["text"] == "Deleted file: clip.mp4"
    assert "This folder is empty." in context.bot.texts()[-1]


async def test_old_browser_buttons_still_work(download_dir):
    _write(download_dir, "Movies/clip.mp4")
    context = make_context()
    token = tb.encode_path("Movies")

    await tb.on_button(callback_update(context, f"fb:dirinfo:0:{token}"), context)
    assert "⋯ <b>/Movies</b>" in context.bot.texts()[-1]
    await tb.on_button(callback_update(context, f"fb:dir:0:{token}"), context)
    assert "📁 <b>Files › Movies</b>" in context.bot.texts()[-1]


# ---------------------------------------------------------------- selection


async def test_select_toggle_all_and_delete(download_dir):
    for name in ("a.txt", "b.txt", "c.txt"):
        _write(download_dir, name)
    context = make_context()

    await tb.on_button(callback_update(context, "fb:sel:0:"), context)
    screen = context.bot.calls[-1]
    assert "0 of 3 selected" in screen.kwargs["text"]

    await tb.on_button(callback_update(context, _button(screen, "b.txt").callback_data), context)
    assert "1 of 3 selected" in context.bot.texts()[-1]
    assert "✅ 📄 \u2068b.txt\u2069 · 1 B" in [b.text for b in _buttons(context.bot.calls[-1])]
    await tb.on_button(callback_update(context, "fb:sall"), context)
    screen = context.bot.calls[-1]
    assert "3 of 3 selected" in screen.kwargs["text"]
    assert [b.text for b in _buttons(screen) if b.text.startswith(("📤", "📦", "🗑"))] == [
        "📤 Send 3",
        "📦 Zip 3",
        "🗑 Delete 3",
    ]

    await tb.on_button(callback_update(context, "fb:sdel"), context)
    assert "Permanently delete 3 file(s)" in context.bot.texts()[-1]
    await tb.on_button(callback_update(context, "fb:sdelyes"), context)

    assert not any(download_dir.iterdir())
    assert "There are no files directly in this folder." in context.bot.texts()[-1]


async def test_selection_actions_need_a_selection(download_dir):
    _write(download_dir, "a.txt")
    context = make_context()
    await tb.on_button(callback_update(context, "fb:sel:0:"), context)

    update = callback_update(context, "fb:sup")
    await tb.on_button(update, context)

    assert "Select at least one file" in update.callback_query.answers[0]["text"]


# ---------------------------------------------------------------- archives


@pytest.fixture
def sent_documents(monkeypatch):
    sent = []

    async def fake_send(context, chat_id, archive_path, caption):
        sent.append((archive_path.name, archive_path.stat().st_size))
        return True

    monkeypatch.setattr(tb, "send_archive_document", fake_send)
    return sent


async def test_zip_selected_with_default_name_uploads_parts_as_they_are_made(
    download_dir, sent_documents
):
    import os

    for name in ("a.bin", "b.bin"):
        _write(download_dir, f"Pack/{name}", os.urandom(6000))
    await user_settings.update_setting(1, "zip_part_size", 4096)
    context = make_context()

    await tb.on_button(callback_update(context, f"fb:sel:0:{tb.encode_path('Pack')}"), context)
    await tb.on_button(callback_update(context, "fb:sall"), context)
    await tb.on_button(callback_update(context, "fb:szip"), context)
    prompt = context.bot.calls[-1]
    assert "Zip 2 file(s)" in prompt.kwargs["text"] and "<code>Pack</code>" in prompt.kwargs["text"]

    await tb.on_button(callback_update(context, "zipname:default"), context)
    await _drain()

    assert len(sent_documents) >= 3  # ~12 KB of random data in 4 KB parts
    assert all(name.startswith("Pack.zip.") for name, _ in sent_documents)
    # Each part was removed right after it was sent.
    assert not [p for p in download_dir.iterdir() if p.name.startswith("Pack.zip")]
    final = context.bot.texts()[-1]
    assert "Archive sent: Pack" in final and "uploaded as they were created" in final


async def test_zip_name_can_be_typed(download_dir, sent_documents):
    _write(download_dir, "notes.txt", b"hello")
    context = make_context()

    await tb.on_button(callback_update(context, "zip_menu:zip_all"), context)
    await tb.on_text(text_update(context, "my notes"), context)
    await _drain()

    assert [name for name, _ in sent_documents] == ["my notes.zip"]
    assert "Archive sent: my notes" in context.bot.texts()[-1]
    assert 1 not in tb.pending_zip_name_sessions


async def test_archive_menu_and_old_zip_buttons(download_dir):
    _write(download_dir, "notes.txt", b"hello")
    context = make_context()

    await tb.on_button(callback_update(context, "nav:archive"), context)
    assert "Download folder: 1 files" in context.bot.texts()[-1]

    await tb.on_button(callback_update(context, "zip_menu:select"), context)
    assert "☑️ <b>Select files</b>" in context.bot.texts()[-1]
