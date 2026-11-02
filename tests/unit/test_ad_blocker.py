"""The page finder's ad blocker: EasyList-style rules, caching, and graceful fallbacks."""

import os
import time

import httpx
import pytest

from app.services.ad_blocker import AdBlocker

pytest.importorskip("adblock")

RULES = "[Adblock Plus 2.0]\n||ads.example^\n/banner_*.mp4\n"


def _transport(calls, body=RULES, status=200):
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


def test_blocks_matching_requests_only(tmp_path):
    calls = []
    blocker = AdBlocker(
        tmp_path, ("https://lists.example/easylist.txt",), transport=_transport(calls)
    )

    assert blocker.load()
    assert blocker.should_block("https://ads.example/x.js", "https://site.example/", "script")
    assert blocker.should_block(
        "https://cdn.example/banner_300.mp4", "https://site.example/", "media"
    )
    assert not blocker.should_block(
        "https://cdn.example/stream/master.m3u8", "https://site.example/", "xhr"
    )
    assert (tmp_path / "easylist.txt").read_text() == RULES


def test_lists_are_cached_for_a_week(tmp_path):
    calls = []
    (tmp_path / "easylist.txt").write_text(RULES)
    blocker = AdBlocker(
        tmp_path, ("https://lists.example/easylist.txt",), transport=_transport(calls)
    )
    assert blocker.load() and calls == []  # fresh copy: no download

    old = time.time() - 8 * 24 * 3600
    os.utime(tmp_path / "easylist.txt", (old, old))
    blocker = AdBlocker(
        tmp_path, ("https://lists.example/easylist.txt",), transport=_transport(calls)
    )
    assert blocker.load() and len(calls) == 1


def test_a_failed_download_keeps_the_old_list_or_turns_blocking_off(tmp_path):
    calls = []
    failing = _transport(calls, body="nope", status=500)
    blocker = AdBlocker(tmp_path, ("https://lists.example/easylist.txt",), transport=failing)
    assert blocker.load() is False
    assert blocker.should_block("https://ads.example/x.js", "https://s/", "script") is False

    (tmp_path / "easylist.txt").write_text(RULES)
    old = time.time() - 8 * 24 * 3600
    os.utime(tmp_path / "easylist.txt", (old, old))
    blocker = AdBlocker(tmp_path, ("https://lists.example/easylist.txt",), transport=failing)
    assert blocker.load()  # stale, but better than nothing
    assert blocker.should_block("https://ads.example/x.js", "https://s/", "script")
