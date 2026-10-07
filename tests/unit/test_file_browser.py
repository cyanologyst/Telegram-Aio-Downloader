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


def test_browser_lists_items_with_sizes_and_number_buttons():
    tokens = {}
    entries = [
        {"name": "Show", "rel_path": "Show", "is_dir": True, "size": 0, "count": 3},
        {"name": "a <b>.mkv", "rel_path": "a <b>.mkv", "is_dir": False, "size": 2048},
    ]

    text, markup = file_views.browser_screen(
        "", entries, 0, lambda p: tokens.setdefault(p, f"t{len(tokens)}")
    )

    assert "1. 📁 Show — 3 items" in text
    assert "2. 🎞 a &lt;b&gt;.mkv — 2.0 KB" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert labels[:2] == ["1", "2"]
    assert "⬆ Up" not in labels  # already at the root
    assert len(markup.inline_keyboard) <= 4  # compact: numbers, select/more, nav


def test_browser_pages_and_empty_folder():
    entries = [
        {"name": f"f{i}", "rel_path": f"f{i}", "is_dir": False, "size": 1} for i in range(20)
    ]
    text, markup = file_views.browser_screen("x", entries, 2, lambda p: p)
    assert "page 3/3" in text and "17. " in text
    empty_text, _ = file_views.browser_screen("x", [], 0, lambda p: p)
    assert "This folder is empty." in empty_text


# ---------------------------------------------------------------- browsing


async def test_open_folder_then_file_then_back(download_dir):
    _write(download_dir, "Movies/clip.mp4", b"v" * 10)
    context = make_context()

    await tb.files_cmd(text_update(context, "/files"), context)
    root = context.bot.calls[-1]
    assert "1. 📁 Movies — 1 items" in root.kwargs["text"]

    await tb.on_button(callback_update(context, _button(root, "1").callback_data), context)
    folder = context.bot.calls[-1]
    assert "📁 <b>/Movies</b>" in folder.kwargs["text"]

    await tb.on_button(callback_update(context, _button(folder, "1").callback_data), context)
    detail = context.bot.calls[-1]
    assert "🎞 <b>clip.mp4</b>" in detail.kwargs["text"]
    labels = [b.text for b in _buttons(detail)]
    assert "🎬 Convert" in labels and "📸 Thumbnails" in labels

    await tb.on_button(callback_update(context, _button(detail, "Folder").callback_data), context)
    assert "📁 <b>/Movies</b>" in context.bot.texts()[-1]


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
    assert "📁 <b>/Movies</b>" in context.bot.texts()[-1]


# ---------------------------------------------------------------- selection


async def test_select_toggle_all_and_delete(download_dir):
    for name in ("a.txt", "b.txt", "c.txt"):
        _write(download_dir, name)
    context = make_context()

    await tb.on_button(callback_update(context, "fb:sel:0:"), context)
    screen = context.bot.calls[-1]
    assert "0 of 3 selected" in screen.kwargs["text"]

    await tb.on_button(callback_update(context, _button(screen, "2").callback_data), context)
    assert "1 of 3 selected" in context.bot.texts()[-1]
    await tb.on_button(callback_update(context, "fb:sall"), context)
    screen = context.bot.calls[-1]
    assert "3 of 3 selected" in screen.kwargs["text"]
    assert [b.text for b in _buttons(screen) if b.text.startswith(("📤", "📦", "🗑"))] == [
        "📤 Upload 3",
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
