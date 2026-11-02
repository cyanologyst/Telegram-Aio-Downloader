"""Ad blocking for the page finder's browser: Brave's engine with EasyList.

Streaming sites load banner videos, pop-ups and "tab-under" redirects that
get in the way of starting the real player (and can be mistaken for it).
Requests matching EasyList are refused before they leave the browser.

The list is downloaded on first use and cached for a week; without the
``adblock`` package or the list, ``should_block`` returns False and the page
finder relies on its own ad rules.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_LISTS = ("https://easylist.to/easylist/easylist.txt",)
MAX_AGE = 7 * 24 * 3600

# Playwright resource types -> the request types filter lists use.
RESOURCE_TYPES = {
    "document": "sub_frame",  # the top page itself is never checked
    "stylesheet": "stylesheet",
    "image": "image",
    "media": "media",
    "font": "font",
    "script": "script",
    "texttrack": "other",
    "xhr": "xmlhttprequest",
    "fetch": "xmlhttprequest",
    "eventsource": "other",
    "websocket": "websocket",
    "manifest": "other",
    "ping": "ping",
    "other": "other",
}


class AdBlocker:
    def __init__(
        self,
        cache_dir: Path,
        lists: tuple[str, ...] = DEFAULT_LISTS,
        *,
        transport: httpx.BaseTransport | None = None,  # tests
    ):
        self.cache_dir = Path(cache_dir)
        self.lists = lists
        self.transport = transport
        self._engine: Any = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("adblock") is not None

    def _list_path(self, url: str) -> Path:
        name = url.rstrip("/").rsplit("/", 1)[-1] or "list.txt"
        return self.cache_dir / name

    def _download(self, url: str, path: Path) -> None:
        with httpx.Client(timeout=30, follow_redirects=True, transport=self.transport) as client:
            response = client.get(url)
            response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_text(response.text, encoding="utf-8")
        tmp.replace(path)

    def load(self) -> bool:
        """Build the engine (downloading stale lists first). False when unavailable."""
        with self._lock:
            if self._engine is not None and time.time() - self._loaded_at < MAX_AGE:
                return True
            if not self.available():
                return False
            import adblock

            filters = adblock.FilterSet()
            loaded = 0
            for url in self.lists:
                path = self._list_path(url)
                stale = not path.is_file() or time.time() - path.stat().st_mtime > MAX_AGE
                if stale:
                    try:
                        self._download(url, path)
                    except Exception as exc:  # keep an old copy if there is one
                        logger.warning("Could not download ad list %s: %s", url, exc)
                if path.is_file():
                    filters.add_filter_list(path.read_text(encoding="utf-8", errors="replace"))
                    loaded += 1
            if not loaded:
                return False
            self._engine = adblock.Engine(filters)
            self._loaded_at = time.time()
            return True

    def should_block(self, url: str, source_url: str, resource_type: str) -> bool:
        if self._engine is None or not url.startswith(("http://", "https://")):
            return False
        kind = RESOURCE_TYPES.get(resource_type, "other")
        try:
            return bool(self._engine.check_network_urls(url, source_url or url, kind).matched)
        except Exception:
            return False
