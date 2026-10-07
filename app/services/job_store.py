"""Persist the download job list in SQLite so it survives restarts.

Jobs are plain dicts owned by the bot; this stores the JSON-friendly part of
each one (runtime objects such as processes and tasks are dropped). Uses the
standard-library sqlite3 module, so there is nothing extra to install.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Runtime-only values that must not (and cannot) be stored.
RUNTIME_KEYS = {"process", "task", "playlist"}

FINISHED = {"completed", "failed", "cancelled"}


def _storable(job: dict[str, Any]) -> dict[str, Any]:
    clean = {}
    for key, value in job.items():
        if key in RUNTIME_KEYS:
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            continue
        clean[key] = value
    return clean


class JobStore:
    def __init__(self, path: Path, keep_finished: int = 200) -> None:
        self.path = Path(path)
        self.keep_finished = keep_finished
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            " id INTEGER PRIMARY KEY,"
            " status TEXT NOT NULL,"
            " data TEXT NOT NULL,"
            " updated REAL NOT NULL)"
        )
        self._db.commit()

    def sync(self, jobs: Iterable[dict[str, Any]]) -> None:
        """Make the table match ``jobs``: upsert every job, drop the ones gone from it."""
        now = time.time()
        rows = []
        for job in jobs:
            data = _storable(job)
            rows.append((int(job["id"]), str(job.get("status", "")), json.dumps(data), now))
        ids = [row[0] for row in rows]
        with self._lock:
            self._db.executemany(
                "INSERT INTO jobs (id, status, data, updated) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, data=excluded.data, "
                "updated=excluded.updated",
                rows,
            )
            if ids:
                marks = ",".join("?" * len(ids))
                self._db.execute(f"DELETE FROM jobs WHERE id NOT IN ({marks})", ids)
            else:
                self._db.execute("DELETE FROM jobs")
            self._db.commit()

    def load(self) -> list[dict[str, Any]]:
        """All stored jobs, oldest first, keeping only the newest finished ones."""
        with self._lock:
            rows = self._db.execute("SELECT id, status, data FROM jobs ORDER BY id").fetchall()
        jobs = []
        for _id, _status, data in rows:
            try:
                jobs.append(json.loads(data))
            except json.JSONDecodeError:
                logger.warning("Skipping unreadable stored job %s", _id)
        finished = [j for j in jobs if j.get("status") in FINISHED]
        drop = {j["id"] for j in finished[: max(0, len(finished) - self.keep_finished)]}
        return [j for j in jobs if j["id"] not in drop]

    def close(self) -> None:
        with self._lock:
            self._db.close()
