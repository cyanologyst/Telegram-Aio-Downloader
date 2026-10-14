"""How many downloads run at once: the per-user ``max_concurrent_downloads`` setting.

Every download job takes a slot before it starts transferring and gives it
back when it ends. Jobs over the limit wait in first-come order and show
"Queued (position n)". aria2's own ``--max-concurrent-downloads=8`` stays as
the transport cap above this.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

MIN_SLOTS = 1
MAX_SLOTS = 8
DEFAULT_SLOTS = 8


def limit_of(settings: dict[str, Any]) -> int:
    """The user's limit, clamped to 1-8 (anything unreadable is the default)."""
    try:
        value = int(settings.get("max_concurrent_downloads", DEFAULT_SLOTS))
    except (TypeError, ValueError):
        return DEFAULT_SLOTS
    return max(MIN_SLOTS, min(MAX_SLOTS, value))


class DownloadSlots:
    """First-come download slots, one pool per user.

    ``acquire`` polls instead of waiting on a condition so a limit raised in
    Settings lets queued jobs start without any extra signalling.
    """

    poll_seconds = 1.0

    def __init__(self) -> None:
        self._active: dict[int, set[int]] = {}
        self._waiting: dict[int, list[int]] = {}

    def active(self, user_id: int) -> int:
        return len(self._active.get(user_id, ()))

    def holds(self, user_id: int, job_id: int) -> bool:
        return job_id in self._active.get(user_id, ())

    def position(self, user_id: int, job_id: int) -> int | None:
        """1-based place of a waiting job in its user's queue, or None."""
        waiting = self._waiting.get(user_id, [])
        return waiting.index(job_id) + 1 if job_id in waiting else None

    def try_acquire(self, user_id: int, job_id: int, limit: int) -> bool:
        """Take a slot now if one is free and nobody is queued ahead."""
        if self.holds(user_id, job_id):
            return True
        waiting = self._waiting.get(user_id, [])
        if waiting and waiting[0] != job_id:
            return False
        if self.active(user_id) >= limit:
            return False
        if waiting:
            waiting.pop(0)
        self._active.setdefault(user_id, set()).add(job_id)
        return True

    async def acquire(
        self,
        user_id: int,
        job_id: int,
        limit: Callable[[], int],
        on_wait: Callable[[int], Any] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Wait for a slot; False if ``cancelled()`` turned true first.

        ``on_wait(position)`` is called (and awaited if it is a coroutine)
        on every poll while the job waits.
        """
        if self.try_acquire(user_id, job_id, limit()):
            return True
        waiting = self._waiting.setdefault(user_id, [])
        if job_id not in waiting:
            waiting.append(job_id)
        try:
            while not self.try_acquire(user_id, job_id, limit()):
                if cancelled is not None and cancelled():
                    if job_id in waiting:
                        waiting.remove(job_id)
                    return False
                if on_wait is not None:
                    result = on_wait(self.position(user_id, job_id))
                    if asyncio.iscoroutine(result):
                        await result
                await asyncio.sleep(self.poll_seconds)
        except BaseException:
            if job_id in waiting:
                waiting.remove(job_id)
            raise
        return True

    def release(self, user_id: int, job_id: int) -> None:
        self._active.get(user_id, set()).discard(job_id)
        waiting = self._waiting.get(user_id, [])
        if job_id in waiting:
            waiting.remove(job_id)
