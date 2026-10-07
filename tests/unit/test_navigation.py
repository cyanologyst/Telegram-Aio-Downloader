"""Menus, settings hub and status dashboard navigation."""

import pytest

import app.bot.telegram_bot as tb
from app.bot.views import home as home_views
from app.bot.views import settings as settings_views
from app.services import user_settings
from tests.fakes import callback_update, make_context, text_update


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "status_messages", {})
    monkeypatch.setattr(tb, "search_ui", None)


def _buttons(call):
    markup = call.kwargs.get("reply_markup")
    return [b for row in markup.inline_keyboard for b in row] if markup else []


def _all_callback_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ---------------------------------------------------------------- views


def test_every_screen_has_a_way_home():
    s = user_settings.DEFAULT_SETTINGS
    screens = [
        home_views.help_screen("https://example.test"),
        home_views.sites_screen("https://example.test"),
        settings_views.settings_screen(s),
        settings_views.archive_settings_screen(s),
        settings_views.archive_choice_screen("x", [("a", "zip_set_value:zip_method:zip")], "nav:x"),
    ]
    for text, markup in screens:
        assert "nav:home" in _all_callback_data(markup), text[:40]


def test_home_shows_active_jobs_and_disk_space():
    text, markup = home_views.home_screen(2, 5 * 1024**3, home_views.MiniApp())
    assert "Active downloads: <b>2</b>" in text
    assert "5.0 GB" in text
    data = _all_callback_data(markup)
    assert {"nav:status", "fb:list:0:", "srch:p:", "nav:settings", "nav:help"} <= set(data)


def test_video_default_cycles_through_every_choice():
    seen, value = [], "ask"
    for _ in settings_views.VIDEO_DEFAULTS:
        seen.append(value)
        value = settings_views.next_video_default(value)
    assert value == "ask" and seen == settings_views.VIDEO_DEFAULTS
    assert settings_views.next_video_default("garbage") == "best"


# ---------------------------------------------------------------- handlers


@pytest.mark.parametrize("data", ["nav:home", "menu_home", "menu:downloads", "menu:tools"])
async def test_home_buttons_edit_in_place(data):
    context = make_context()
    update = callback_update(context, data)

    await tb.on_button(update, context)

    call = context.bot.calls[-1]
    assert call.method == "edit_text"
    assert call.kwargs["parse_mode"] == "HTML"
    assert call.kwargs["text"].startswith("🏠 <b>Menu</b>")


async def test_settings_toggle_saves_and_redraws():
    context = make_context()

    await tb.on_button(callback_update(context, "set:auto_upload_after_download"), context)
    await tb.on_button(callback_update(context, "set:video_default"), context)

    saved = user_settings.get_user_settings(1)
    assert saved["auto_upload_after_download"] is True
    assert saved["video_default"] == "best"
    assert "Upload to Saved Messages when done: On" in context.bot.texts()[-1]


async def test_archive_settings_back_returns_where_you_came_from():
    context = make_context()

    await tb.on_button(callback_update(context, "nav:archive"), context)
    await tb.on_button(callback_update(context, "zip_menu:settings"), context)
    assert "nav:archive" in _all_callback_data(context.bot.calls[-1].kwargs["reply_markup"])

    await tb.on_button(callback_update(context, "nav:archive_settings"), context)
    assert "nav:settings" in _all_callback_data(context.bot.calls[-1].kwargs["reply_markup"])


async def test_archive_value_pickers_save_and_return():
    context = make_context()

    await tb.on_button(callback_update(context, "zip_setting:method"), context)
    assert "Archive format" in context.bot.texts()[-1]
    await tb.on_button(callback_update(context, "zip_set_value:compression_level:7"), context)

    assert user_settings.get_user_settings(1)["compression_level"] == 7
    assert "Compression level: 7/9" in context.bot.texts()[-1]


async def test_old_language_buttons_explain_and_show_settings():
    context = make_context()
    update = callback_update(context, "set_lang:fa")

    await tb.on_button(update, context)

    assert "English-only" in update.callback_query.answers[0]["text"]
    assert context.bot.texts()[-1].startswith("⚙️ <b>Settings</b>")


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (home_views.KEY_MENU, "🏠 <b>Menu</b>"),
        (home_views.KEY_SETTINGS, "⚙️ <b>Settings</b>"),
        (home_views.KEY_HELP, "❓ <b>How to use</b>"),
        ("🛠 Tools", "🏠 <b>Menu</b>"),  # old keyboard label
    ],
)
async def test_keyboard_labels_open_their_screens(label, expected):
    context = make_context()

    await tb.on_text(text_update(context, label), context)

    assert context.bot.texts()[-1].startswith(expected)


async def test_status_moves_to_the_bottom_and_removes_the_old_one():
    context = make_context()

    await tb.on_text(text_update(context, home_views.KEY_STATUS), context)
    first_id = tb.status_messages[1]["message_id"]
    await tb.on_text(text_update(context, home_views.KEY_STATUS), context)

    assert tb.status_messages[1]["message_id"] != first_id
    deleted = context.bot.called("delete_message")
    assert deleted and deleted[-1].kwargs["message_id"] == first_id
    assert "Nothing downloading" in context.bot.texts()[-1]


async def test_start_installs_the_keyboard():
    context = make_context()

    await tb.start_cmd(text_update(context, "/start"), context)

    call = context.bot.called("reply_text")[-1]
    labels = [
        b if isinstance(b, str) else b.text
        for row in call.kwargs["reply_markup"].keyboard
        for b in row
    ]
    assert {home_views.KEY_STATUS, home_views.KEY_FILES, home_views.KEY_SEARCH} <= set(labels)
