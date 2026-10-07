"""Answer each Telegram callback query exactly once.

Telegram accepts only one answerCallbackQuery per button press; a second one
fails with "query is too old ... or query id is invalid". The bot used to
answer at the top of every handler and again later to show a result alert,
so the second call raised and the handler's error path replaced the user's
screen with an error message.

``answer_once`` remembers which queries were answered (PTB's CallbackQuery is
immutable, so the ids are tracked here), and ``auto_answer`` makes sure every
handler answers before it returns so the button's loading spinner stops.

Pattern: validate first and answer with an alert when something is wrong;
call ``answer_once(query)`` before slow work; let ``auto_answer`` cover the rest.
"""

from __future__ import annotations

import functools
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from telegram.error import NetworkError

logger = logging.getLogger(__name__)

_MAX_TRACKED = 4096
_answered: OrderedDict[str, None] = OrderedDict()

Handler = TypeVar("Handler", bound=Callable[..., Awaitable[Any]])


def _query_key(query: Any) -> str:
    return str(getattr(query, "id", None) or id(query))


def was_answered(query: Any) -> bool:
    return _query_key(query) in _answered


async def answer_once(query: Any, text: str | None = None, *, show_alert: bool = False) -> bool:
    """Answer ``query`` unless it was already answered. Returns True if this call answered."""
    key = _query_key(query)
    if key in _answered:
        if text:
            logger.debug("Callback %s already answered; dropping notice %r", key, text)
        return False
    _answered[key] = None
    while len(_answered) > _MAX_TRACKED:
        _answered.popitem(last=False)
    try:
        await query.answer(text, show_alert=show_alert)
    except NetworkError as exc:
        # BadRequest (a subclass): usually the query expired while the handler was
        # busy. A dropped connection must not abort the handler either: the
        # answer only stops the button's spinner, the work that follows matters.
        logger.debug("Could not answer callback %s: %s", key, exc)
        return False
    return True


def auto_answer(handler: Handler) -> Handler:
    """Decorate a callback handler so its query is always answered on exit.

    Works for functions ``(update, context)`` and methods ``(self, update, context)``.
    """

    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        update = args[-2] if len(args) >= 2 else kwargs.get("update")
        try:
            return await handler(*args, **kwargs)
        finally:
            query = getattr(update, "callback_query", None)
            if query is not None:
                await answer_once(query)

    return wrapper  # type: ignore[return-value]
