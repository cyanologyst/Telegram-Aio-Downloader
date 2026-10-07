"""Lightweight fakes for driving the real Telegram handlers in tests.

The fakes record every Bot API call instead of talking to Telegram, and they
mimic the Telegram behaviours the bot has tripped over before - most notably
that a callback query can be answered only once.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from telegram.error import BadRequest

_message_ids = itertools.count(1000)


@dataclass
class Call:
    method: str
    kwargs: dict[str, Any]


class FakeBot:
    """Records Bot API calls; any method name is accepted."""

    def __init__(self) -> None:
        self.calls: list[Call] = []

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)

        async def record(*args: Any, **kwargs: Any):
            if args:
                kwargs = {**{f"arg{i}": a for i, a in enumerate(args)}, **kwargs}
            self.calls.append(Call(method, kwargs))
            if method == "send_message":
                return FakeMessage(self, chat_id=kwargs.get("chat_id", 0), text=kwargs.get("text"))
            return True

        return record

    def called(self, method: str) -> list[Call]:
        return [call for call in self.calls if call.method == method]

    def texts(self) -> list[str]:
        return [
            str(call.kwargs.get("text"))
            for call in self.calls
            if call.method in {"send_message", "edit_message_text", "reply_text", "edit_text"}
        ]


class FakeMessage:
    def __init__(
        self,
        bot: FakeBot,
        chat_id: int,
        text: str | None = None,
        user_id: int = 0,
        message_id: int | None = None,
    ) -> None:
        self._bot = bot
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id)
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.message_id = message_id if message_id is not None else next(_message_ids)
        self.document = None

    async def reply_text(self, text: str, **kwargs: Any) -> FakeMessage:
        self._bot.calls.append(Call("reply_text", {"text": text, **kwargs}))
        return FakeMessage(self._bot, self.chat_id, text)

    async def edit_text(self, text: str, **kwargs: Any) -> FakeMessage:
        self._bot.calls.append(
            Call("edit_text", {"text": text, "message_id": self.message_id, **kwargs})
        )
        self.text = text
        return self


class FakeCallbackQuery:
    def __init__(self, bot: FakeBot, data: str, user_id: int, message: FakeMessage) -> None:
        self._bot = bot
        self.id = str(next(_message_ids))
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = message
        self.answers: list[dict[str, Any]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False, **_: Any) -> bool:
        # Telegram rejects a second answerCallbackQuery for the same query.
        if self.answers:
            raise BadRequest("Query is too old and response timeout expired or query id is invalid")
        self.answers.append({"text": text, "show_alert": show_alert})
        return True

    async def edit_message_text(self, text: str, **kwargs: Any) -> FakeMessage:
        return await self.message.edit_text(text, **kwargs)

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs: Any) -> FakeMessage:
        self._bot.calls.append(
            Call(
                "edit_message_reply_markup",
                {"reply_markup": reply_markup, "message_id": self.message.message_id, **kwargs},
            )
        )
        return self.message


@dataclass
class FakeContext:
    bot: FakeBot
    args: list[str] = field(default_factory=list)
    user_data: dict[str, Any] = field(default_factory=dict)
    chat_data: dict[str, Any] = field(default_factory=dict)
    error: BaseException | None = None

    @property
    def application(self) -> SimpleNamespace:
        return SimpleNamespace(bot=self.bot)


def make_context(bot: FakeBot | None = None, **kwargs: Any) -> FakeContext:
    return FakeContext(bot=bot or FakeBot(), **kwargs)


def text_update(
    context: FakeContext, text: str, user_id: int = 1, chat_id: int | None = None
) -> SimpleNamespace:
    chat_id = chat_id if chat_id is not None else user_id
    message = FakeMessage(context.bot, chat_id, text, user_id=user_id)
    return SimpleNamespace(
        message=message,
        effective_message=message,
        callback_query=None,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
    )


def callback_update(
    context: FakeContext, data: str, user_id: int = 1, chat_id: int | None = None
) -> SimpleNamespace:
    chat_id = chat_id if chat_id is not None else user_id
    message = FakeMessage(context.bot, chat_id, "previous screen", user_id=user_id)
    query = FakeCallbackQuery(context.bot, data, user_id, message)
    return SimpleNamespace(
        message=None,
        effective_message=message,
        callback_query=query,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
    )
