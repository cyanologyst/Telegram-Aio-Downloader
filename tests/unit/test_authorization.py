"""The bot must ignore everyone except the configured owner(s)."""

from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

import app.bot.telegram_bot as tb
from tests.fakes import callback_update, make_context, text_update

OWNER = 111
STRANGER = 999


@pytest.fixture(autouse=True)
def owner_only(monkeypatch):
    monkeypatch.setattr(tb, "ALLOWED_USER_IDS", {OWNER})


async def test_gate_lets_the_owner_through():
    context = make_context()
    await tb.authorization_gate(text_update(context, "hi", user_id=OWNER), context)


@pytest.mark.parametrize(
    "make_update",
    [
        lambda ctx: text_update(ctx, "magnet:?xt=urn:btih:abc", user_id=STRANGER),
        lambda ctx: callback_update(ctx, "fb:deleteall_yes:0:", user_id=STRANGER),
    ],
)
async def test_gate_stops_strangers_silently(make_update):
    context = make_context()
    with pytest.raises(ApplicationHandlerStop):
        await tb.authorization_gate(make_update(context), context)
    assert context.bot.calls == []


async def test_gate_stops_updates_without_a_user():
    context = make_context()
    update = SimpleNamespace(effective_user=None)
    with pytest.raises(ApplicationHandlerStop):
        await tb.authorization_gate(update, context)


def test_empty_allow_list_authorizes_nobody(monkeypatch):
    monkeypatch.setattr(tb, "ALLOWED_USER_IDS", set())
    assert not tb.is_authorized_user(OWNER)


def test_main_refuses_to_start_without_allowed_users(monkeypatch):
    monkeypatch.setattr(tb, "BOT_TOKEN", "1:token")
    monkeypatch.setattr(tb, "API_ID", 1)
    monkeypatch.setattr(tb, "API_HASH", "hash")
    monkeypatch.setattr(tb, "ALLOWED_USER_IDS", set())
    with pytest.raises(RuntimeError, match="ALLOWED_USER_IDS"):
        tb.main()
