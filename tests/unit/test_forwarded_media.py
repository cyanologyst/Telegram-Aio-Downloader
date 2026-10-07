"""Forwarded media is only taken from the owner's chat with the bot."""

from pathlib import Path

import pytest
from pyrogram import types

from app.handlers import forwarded_media as fm

BOT_ID = 4242
OWNER = 111


def _message(chat_id=BOT_ID, outgoing=True, forwarded=True, photo=True):
    # Real Message objects: Kurigram's filters check isinstance(update, Message).
    message = types.Message.__new__(types.Message)
    message.__dict__.update(
        id=7,
        chat=types.Chat(id=chat_id),
        from_user=types.User(id=OWNER, is_self=True),
        outgoing=outgoing,
        forward_date=1 if forwarded else None,
        forward_origin=object() if forwarded else None,
        photo=object() if photo else None,
        video=None,
        document=None,
        audio=None,
        voice=None,
        animation=None,
        video_note=None,
        sticker=None,
    )
    return message


class Notifier:
    def __init__(self):
        self.sent = []

    async def __call__(self, user_id, text, message_id):
        self.sent.append((user_id, text, message_id))
        return message_id or 55


def _write_fake_photo(file_name):
    Path(file_name).write_bytes(b"jpeg")


class FakeClient:
    async def download_media(self, message, file_name):
        _write_fake_photo(file_name)


def _filter():
    return fm.filters.chat(BOT_ID) & fm.filters.outgoing & fm.filters.forwarded & fm.MEDIA_FILTER


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_message(), True),
        (_message(chat_id=999), False),  # forwarded to a friend
        (_message(outgoing=False), False),  # the bot's own messages
        (_message(forwarded=False), False),
        (_message(photo=False), False),
    ],
)
async def test_filter_only_matches_owner_forwards_into_bot_chat(message, expected):
    assert bool(await _filter()(None, message)) is expected


def test_bot_id_is_the_token_prefix():
    assert fm.bot_id_from_token("4242:AAH-secret") == BOT_ID
    with pytest.raises(ValueError):
        fm.bot_id_from_token("not-a-token")


async def test_download_reports_through_the_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fm, "get_user_settings", lambda _uid: {"auto_download_forwarded_posts": True}
    )
    notify = Notifier()
    callback = fm.make_forwarded_media_callback(FakeClient(), tmp_path, notify)

    await callback(None, _message())

    files = list(tmp_path.iterdir())
    assert len(files) == 1 and files[0].name.endswith("photo_7.jpg")
    assert [m[2] for m in notify.sent] == [None, 55]  # sent, then edited in place
    assert notify.sent[-1][1].startswith("✅ Forwarded photo downloaded")
    assert all(m[0] == OWNER for m in notify.sent)


async def test_disabled_setting_explains_how_to_enable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        fm, "get_user_settings", lambda _uid: {"auto_download_forwarded_posts": False}
    )
    notify = Notifier()
    callback = fm.make_forwarded_media_callback(FakeClient(), tmp_path, notify)

    await callback(None, _message())

    assert list(tmp_path.iterdir()) == []
    assert len(notify.sent) == 1 and "auto-download is off" in notify.sent[0][1]
