"""Decide when a progress message (job card, upload, compress) may be edited.

Telegram allows roughly one edit per second per chat and punishes bursts
with FloodWait, so every live message shares one budget:

* an edit is due when progress moved by a milestone (5 %) *and* the minimum
  gap has passed, or when the heartbeat interval (15 s) has passed anyway;
* the minimum gap grows with the number of messages being updated at the
  same time, so N concurrent tasks together stay near one edit per
  ``per_task`` seconds instead of N times that.

Per-message state (time and progress of the last edit) lives in a dict the
caller owns, usually the job itself, so nothing leaks when a job goes away.
The throttle only remembers which messages were active recently.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable, MutableMapping
from typing import Any

AT_KEY = "edit_at"
PCT_KEY = "edit_pct"
PHASE_KEY = "edit_phase"


class AdaptiveThrottle:
    def __init__(
        self,
        *,
        min_interval: float = 4.0,
        per_task: float = 1.5,
        max_interval: float = 15.0,
        milestone: float = 5.0,
        idle_after: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.min_interval = min_interval
        self.per_task = per_task
        self.max_interval = max_interval
        self.milestone = milestone
        self.idle_after = idle_after
        self.clock = clock
        self._seen: dict[Hashable, float] = {}

    # ------------------------------------------------------------ activity
    def touch(self, key: Hashable) -> None:
        self._seen[key] = self.clock()

    def forget(self, key: Hashable) -> None:
        self._seen.pop(key, None)

    def active_count(self) -> int:
        """Messages updated within ``idle_after`` seconds (stale ones are dropped)."""
        cutoff = self.clock() - self.idle_after
        for key in [k for k, at in self._seen.items() if at < cutoff]:
            del self._seen[key]
        return len(self._seen)

    # ------------------------------------------------------------ intervals
    def interval(self, active: int | None = None) -> float:
        """Smallest gap between two edits of one message."""
        active = self.active_count() if active is None else active
        return max(self.min_interval, self.per_task * max(1, active))

    def heartbeat(self, active: int | None = None) -> float:
        """Longest a message goes without an edit while it is being updated."""
        return max(self.max_interval, self.interval(active))

    # ------------------------------------------------------------ decisions
    def due(
        self,
        state: MutableMapping[str, Any],
        key: Hashable,
        progress: float | None = None,
        *,
        phase: Hashable = None,
        force: bool = False,
        final: bool = False,
    ) -> bool:
        """True (and the edit recorded in ``state``) if this message may be edited now.

        ``progress`` is a percentage (0-100) or None when unknown. A change of
        ``phase`` (e.g. the job status) counts like a milestone. ``force`` and
        ``final`` (the last update of a task) always pass.
        """
        self.touch(key)
        now = self.clock()
        last_at = state.get(AT_KEY)
        if not (force or final or last_at is None):
            elapsed = now - float(last_at)
            active = self.active_count()
            if elapsed < self.interval(active):
                return False
            if elapsed < self.heartbeat(active) and state.get(PHASE_KEY) == phase:
                last_pct = state.get(PCT_KEY)
                if progress is None:
                    return False
                if last_pct is not None and abs(float(progress) - float(last_pct)) < self.milestone:
                    return False
        self.mark(state, key, progress, phase=phase)
        return True

    def mark(
        self,
        state: MutableMapping[str, Any],
        key: Hashable,
        progress: float | None = None,
        *,
        phase: Hashable = None,
    ) -> None:
        """Record an edit (or a first send) made outside :meth:`due`."""
        self.touch(key)
        state[AT_KEY] = self.clock()
        state[PCT_KEY] = progress
        state[PHASE_KEY] = phase
