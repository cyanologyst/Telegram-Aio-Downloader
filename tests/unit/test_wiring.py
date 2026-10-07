"""Smoke test: main() wires every handler without touching the network."""

import pytest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, TypeHandler

import app.bot.telegram_bot as tb


@pytest.fixture
def built_app(monkeypatch):
    captured = {}

    def fake_run_polling(self, *args, **kwargs):
        captured["app"] = self

    monkeypatch.setattr(Application, "run_polling", fake_run_polling)
    monkeypatch.setattr(tb, "BOT_TOKEN", "123456:TEST")
    monkeypatch.setattr(tb, "API_ID", 1)
    monkeypatch.setattr(tb, "API_HASH", "hash")
    monkeypatch.setattr(tb, "ALLOWED_USER_IDS", {111})
    monkeypatch.setattr(tb, "WEB_APP_ENABLE", False)
    monkeypatch.setattr(tb, "WEB_DASHBOARD_ENABLE", False)
    monkeypatch.setattr(tb, "search_ui", None)  # main() sets it; restore afterwards
    tb.main()
    return captured["app"]


def _handlers(app):
    return [h for group in app.handlers.values() for h in group]


def test_gate_runs_before_everything(built_app):
    assert min(built_app.handlers) == -1
    assert any(isinstance(h, TypeHandler) for h in built_app.handlers[-1])


def test_commands_are_registered(built_app):
    commands = set()
    for handler in _handlers(built_app):
        if isinstance(handler, CommandHandler):
            commands |= set(handler.commands)
    assert {"start", "status", "cancel", "clear", "search", "tpb", "rarbg", "prowlarr"} <= commands


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("srch:p:", "on_callback"),
        ("lp:abc123:go", "handle_link_request_callback"),
        ("ytdlp_video", "handle_stale_prompt_callback"),
        ("manga_confirm", "handle_stale_prompt_callback"),
        ("up_cancel:upload_1", "on_button"),
        ("fb:list:0:", "on_button"),
        ("tpage:1", "on_button"),
    ],
)
def test_callback_data_reaches_the_right_handler(built_app, data, expected):
    for handler in _handlers(built_app):
        if isinstance(handler, CallbackQueryHandler):
            pattern = handler.pattern
            if pattern is None or pattern.match(data):
                assert handler.callback.__name__ == expected
                return
    raise AssertionError(f"no handler for {data!r}")
