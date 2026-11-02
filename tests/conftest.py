"""Keep test runs out of the real bot log."""

import os
import sys
import tempfile

import pytest

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="tg-bot-test-logs-"))


@pytest.fixture(autouse=True)
def _upload_guard_off(monkeypatch):
    """Uploads ask aria2 and ffprobe whether a file is still downloading.

    Tests elsewhere upload freshly written fake videos and must never reach
    the real aria2, so the guard lets everything through; test_upload_guard
    puts the real one back.
    """
    tb = sys.modules.get("app.bot.telegram_bot")
    if tb is not None:
        async def allow(path):
            return None

        async def no_downloads():
            return []

        monkeypatch.setattr(tb, "upload_refusal", allow)
        monkeypatch.setattr(tb, "unfinished_aria2_downloads", no_downloads)


@pytest.fixture(autouse=True)
def _disk_guard_off(monkeypatch):
    """The disk guard may delete old files under Download/ to make room.

    Tests must not depend on (or purge) the host's real disk, so it is off;
    test_disk_guard turns it back on against a temporary folder.
    """
    tb = sys.modules.get("app.bot.telegram_bot")
    if tb is not None:
        monkeypatch.setattr(tb, "DISK_GUARD_ENABLE", False)
