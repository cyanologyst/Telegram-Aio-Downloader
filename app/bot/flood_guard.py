"""Remember Telegram's per-chat flood control and stop calling into a blocked chat.

When the bot posts or edits too much in one chat, Telegram answers
send/edit calls there with 429 "retry after N" (N can be many hours). The
first 429 for a chat is recorded here, and from then until it lifts
``RetryingRequest`` refuses edits to that chat locally, raising the same
RetryAfter without contacting Telegram.

New messages are not refused for the whole block: Telegram took sends in
the middle of a 14-hour block (while edits stayed refused), and refusing them
all locally left every command unanswered for the rest of it. So while a chat
is blocked a send may go out as a probe once per ``SEND_PROBE_INTERVAL``;
once one is accepted, sends flow again (edits stay paused) until a send is
refused. Calls during a block did not move its end time.

Uploads are unaffected: they go through the user account (Pyrogram), not the
bot. Callback answers carry no chat id, so button taps still get an alert.

Messages the bot could not post meanwhile (upload results, failure notices,
final job cards) are held here per chat and posted once the block lifts.
Blocks and held messages are saved to ``configure()``'s file, so a restart
neither forgets the block (and calls Telegram again) nor loses the results.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# chat_id -> time.time() at which Telegram accepts our sends/edits there again.
_blocked_until: dict[int, float] = {}
# chat_id -> messages to post once the chat's block lifts, oldest first.
_held: dict[int, list[dict]] = {}
# chat_id -> the block (its lift time) the user was already told about elsewhere.
_announced: dict[int, float] = {}
# Blocked chats in which Telegram accepted the latest send (in memory only).
_sends_open: set[int] = set()
# chat_id -> time.time() of the latest send tried in a blocked chat.
_last_probe: dict[int, float] = {}
_state_path: Path | None = None

_UNGUARDED_METHODS = {"sendchataction"}
_SEND_PREFIXES = ("send", "copy", "forward")
SEND_PROBE_INTERVAL = 60.0
# A long block can collect many results; the oldest are dropped past this.
MAX_HELD_PER_CHAT = 30


def configure(path: Path | str | None) -> None:
    """Save state to ``path`` from now on, loading what an earlier run saved there."""
    global _state_path
    _state_path = Path(path) if path else None
    if _state_path is None or not _state_path.is_file():
        return
    try:
        data = json.loads(_state_path.read_text(encoding="utf-8"))
        now = time.time()
        for chat, until in data.get("blocked_until", {}).items():
            if float(until) > now:
                _blocked_until[int(chat)] = max(float(until), _blocked_until.get(int(chat), 0))
        for chat, items in data.get("held", {}).items():
            if isinstance(items, list) and items:
                _held.setdefault(int(chat), []).extend(i for i in items if isinstance(i, dict))
        for chat, until in data.get("announced", {}).items():
            if float(until) > now:
                _announced[int(chat)] = float(until)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        logger.warning(
            "Saved flood-control state %s unreadable (%s); starting fresh", _state_path, exc
        )


def _save() -> None:
    if _state_path is None:
        return
    data = {
        "blocked_until": {str(k): v for k, v in _blocked_until.items()},
        "held": {str(k): v for k, v in _held.items() if v},
        "announced": {str(k): v for k, v in _announced.items()},
    }
    try:
        _state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = _state_path.with_name(_state_path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, _state_path)
    except OSError as exc:
        logger.warning("Could not save flood-control state: %s", exc)


def note(chat_id: int, retry_after: float) -> None:
    """Record that Telegram rejects sends/edits in ``chat_id`` for ``retry_after`` seconds."""
    until = time.time() + float(retry_after)
    previous = _blocked_until.get(chat_id, 0)
    _blocked_until[chat_id] = max(until, previous)
    if _blocked_until[chat_id] - previous > 1:
        _save()


def blocked_for(chat_id: int | None) -> float:
    """Seconds until ``chat_id`` accepts sends/edits again; 0 when it does now."""
    if chat_id is None:
        return 0.0
    remaining = _blocked_until.get(chat_id, 0) - time.time()
    if remaining <= 0:
        if _blocked_until.pop(chat_id, None) is not None:
            _announced.pop(chat_id, None)
            _sends_open.discard(chat_id)
            _last_probe.pop(chat_id, None)
            _save()
        return 0.0
    return remaining


def is_send(method: str) -> bool:
    """Whether a Bot API method (e.g. "sendMessage") posts a new message."""
    return method.lower().startswith(_SEND_PREFIXES)


def can_send(chat_id: int | None) -> bool:
    """Whether a new message to ``chat_id`` would be passed to Telegram now."""
    if chat_id is None:
        return True
    if not blocked_for(chat_id) or chat_id in _sends_open:
        return True
    return time.time() - _last_probe.get(chat_id, 0) >= SEND_PROBE_INTERVAL


def admit(chat_id: int | None, method: str) -> bool:
    """Whether a ``method`` call to ``chat_id`` may go to Telegram (counts send probes)."""
    if chat_id is None:
        return True
    if not blocked_for(chat_id):
        return True
    if not is_send(method) or not can_send(chat_id):
        return False
    _last_probe[chat_id] = time.time()
    return True


def send_result(chat_id: int, accepted: bool) -> bool:
    """Record whether Telegram took a send in a blocked chat; True when sends just reopened."""
    if not accepted:
        _sends_open.discard(chat_id)
        return False
    if chat_id in _sends_open or not blocked_for(chat_id):
        return False
    _sends_open.add(chat_id)
    return True


def sends_open(chat_id: int) -> bool:
    """Whether Telegram is known to take new messages in ``chat_id`` despite its block."""
    return chat_id in _sends_open and bool(blocked_for(chat_id))


def lift_text(chat_id: int) -> str:
    """When the block lifts, e.g. "09:35 UTC (in 14h 23m)"."""
    remaining = blocked_for(chat_id)
    at = datetime.fromtimestamp(time.time() + remaining, tz=UTC)
    hours, rest = divmod(int(remaining), 3600)
    span = f"{hours}h {rest // 60}m" if hours else f"{max(1, rest // 60)}m"
    return f"{at:%H:%M} UTC (in {span})"


def blocked_notice(chat_id: int) -> str:
    """Alert shown on an upload tap while the chat is blocked (alerts allow 200 chars)."""
    return (
        f"Telegram limits this bot's messages here until {lift_text(chat_id)}. "
        "The upload still runs; its result is posted as soon as Telegram allows."
    )


def hold(chat_id: int, item: dict) -> None:
    """Keep ``item`` (a JSON-able description of a message) to post once ``chat_id`` is unblocked."""
    items = _held.setdefault(chat_id, [])
    items.append(item)
    if len(items) > MAX_HELD_PER_CHAT:
        del items[: len(items) - MAX_HELD_PER_CHAT]
    _save()


def held(chat_id: int) -> list[dict]:
    """The messages held for ``chat_id``, oldest first (a copy)."""
    return list(_held.get(chat_id, ()))


def release(chat_id: int, item: dict) -> None:
    """Forget a held message (posted, or given up on)."""
    items = _held.get(chat_id)
    if not items:
        return
    try:
        items.remove(item)
    except ValueError:
        return
    if not items:
        _held.pop(chat_id, None)
    _save()


def chats_with_held() -> list[int]:
    return [chat for chat, items in _held.items() if items]


def take_announcement(chat_id: int) -> bool:
    """True once per block: whether the user should now be told about it some other way."""
    if not blocked_for(chat_id) or chat_id in _announced:
        return False
    _announced[chat_id] = _blocked_until[chat_id]
    _save()
    return True


def guarded_chat(url: str, parameters: dict | None) -> int | None:
    """The chat a Bot API send/edit call targets, or None when it isn't guarded."""
    method = url.rstrip("/").rsplit("/", 1)[-1].lower()
    if not method.startswith((*_SEND_PREFIXES, "edit")) or method in _UNGUARDED_METHODS:
        return None
    raw = (parameters or {}).get("chat_id")
    if isinstance(raw, (int, str)):
        try:
            return int(raw)
        except ValueError:
            pass  # @channel usernames: not tracked
    return None


def retry_after_from(payload: bytes) -> float | None:
    """``retry_after`` from a 429 response body, if present."""
    try:
        data = json.loads(payload)
        return float(data["parameters"]["retry_after"])
    except (ValueError, TypeError, KeyError):
        return None
