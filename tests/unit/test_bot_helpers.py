"""Regression tests for helpers in app.bot.telegram_bot."""

import ast
import pathlib

import pytest
from telegram import InlineKeyboardButton, KeyboardButton

import app.bot.telegram_bot as tb

BOT_SOURCE = pathlib.Path(tb.__file__)


# ---------------------------------------------------------------- languages


def test_language_dicts_have_no_duplicate_keys():
    """A repeated key silently shadows the first value."""
    tree = ast.parse(BOT_SOURCE.read_text(encoding="utf-8"))
    duplicates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seen = set()
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key.value in seen:
                    duplicates.append((key.lineno, key.value))
                seen.add(key.value)
    assert duplicates == []


def test_select_files_is_the_batch_hint_not_the_zip_button():
    assert tb.get_lang(0, "select_files") == "Tap files to select/deselect them."
    assert "Zip" in tb.get_lang(0, "select_files_zip")


def test_language_dicts_have_matching_keys():
    en = set(tb.LANGUAGES["en"])
    fa = set(tb.LANGUAGES["fa"])
    assert en == fa


# ---------------------------------------------------------------- chunks


def _make_chunks(tmp_path, count):
    chunk_dir = tmp_path / "movie_chunks"
    chunk_dir.mkdir()
    chunks = []
    for i in range(1, count + 1):
        part = chunk_dir / f"movie_part_{i:03d}.mkv"
        part.write_bytes(b"x" * 16)
        chunks.append((str(part), i, count))
    return chunk_dir, chunks


def test_cleanup_removes_parts_and_their_folder(tmp_path):
    chunk_dir, chunks = _make_chunks(tmp_path, 3)

    tb.cleanup_file_chunks(chunks)

    assert not chunk_dir.exists()


def test_cleanup_never_touches_an_unsplit_file(tmp_path):
    original = tmp_path / "small.mkv"
    original.write_bytes(b"data")

    tb.cleanup_file_chunks([(str(original), 1, 1)])

    assert original.exists()


def test_cleanup_is_idempotent(tmp_path):
    chunk_dir, chunks = _make_chunks(tmp_path, 2)

    tb.cleanup_file_chunks(chunks)
    tb.cleanup_file_chunks(chunks)  # must not raise

    assert not chunk_dir.exists()


def test_cleanup_leaves_a_folder_that_still_has_other_files(tmp_path):
    chunk_dir, chunks = _make_chunks(tmp_path, 2)
    (chunk_dir / "unrelated.txt").write_text("keep", encoding="utf-8")

    tb.cleanup_file_chunks(chunks)

    assert chunk_dir.exists()
    assert (chunk_dir / "unrelated.txt").exists()


# ---------------------------------------------------------------- mini-app buttons


@pytest.mark.parametrize("url", ["https://example.test", "http://127.0.0.1:5000"])
def test_reply_rows_only_ever_contain_reply_buttons(monkeypatch, url):
    """A ReplyKeyboardMarkup cannot carry an InlineKeyboardButton."""
    monkeypatch.setattr(tb, "WEB_APP_ENABLE", True)
    monkeypatch.setattr(tb, "WEB_APP_URL", url)

    for row in tb.mini_app_reply_rows():
        for button in row:
            assert isinstance(button, KeyboardButton | str)


@pytest.mark.parametrize("url", ["https://example.test", "http://127.0.0.1:5000"])
def test_inline_rows_only_ever_contain_inline_buttons(monkeypatch, url):
    monkeypatch.setattr(tb, "WEB_APP_ENABLE", True)
    monkeypatch.setattr(tb, "WEB_APP_URL", url)

    rows = tb.mini_app_inline_rows()
    assert rows, "an inline mini-app button should be offered for both schemes"
    for row in rows:
        for button in row:
            assert isinstance(button, InlineKeyboardButton)


def test_reply_menu_builds_over_plain_http(monkeypatch):
    """Regression: the HTTP fallback used to inject an invalid reply button."""
    monkeypatch.setattr(tb, "WEB_APP_ENABLE", True)
    monkeypatch.setattr(tb, "WEB_APP_URL", "http://127.0.0.1:5000")

    markup = tb.build_reply_menu(0)

    for row in markup.keyboard:
        for button in row:
            assert "url" not in button.to_dict()


def test_no_mini_app_buttons_when_disabled(monkeypatch):
    monkeypatch.setattr(tb, "WEB_APP_ENABLE", False)
    monkeypatch.setattr(tb, "WEB_APP_URL", "https://example.test")

    assert tb.mini_app_reply_rows() == []
    assert tb.mini_app_inline_rows() == []
