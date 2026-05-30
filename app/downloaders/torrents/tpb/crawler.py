"""Async client for The Pirate Bay via apibay.org API."""

import logging
import os
from typing import List, Dict, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


class TPBCrawler:
    """Search and fetch torrents from The Pirate Bay.

    Uses the unofficial apibay.org JSON API (the same backend
    that powers thepiratebay.org).
    """

    DEFAULT_API_URL = "https://apibay.org"

    # Main TPB categories
    CATEGORIES = {
        "all": "0",
        "audio": "100",
        "video": "200",
        "apps": "300",
        "games": "400",
        "porn": "500",
        "other": "600",
    }

    # Friendly labels for UI
    CATEGORY_LABELS = {
        "all": "🌐 All",
        "audio": "🎵 Audio",
        "video": "🎬 Video",
        "apps": "🖥️ Apps",
        "games": "🎮 Games",
        "porn": "🔞 XXX",
        "other": "📦 Other",
    }

    def __init__(self, api_url: Optional[str] = None):
        self.api_url = (
            api_url or os.getenv("TPB_API_URL", self.DEFAULT_API_URL)
        ).rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    async def search(
        self,
        query: str,
        category: str = "0",
        page: int = 0,
    ) -> List[Dict]:
        """Search TPB. Returns a list of torrent dicts.

        Args:
            query: Search string.
            category: TPB category code (default "0" = all).
            page: Page number (0-based).

        Returns:
            List of torrent result dicts.
        """
        try:
            resp = await self.client.get(
                f"{self.api_url}/q.php",
                params={
                    "q": query,
                    "cat": category,
                    "page": page,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                return data
            return []
        except Exception as exc:
            logger.error("TPB search error: %s", exc)
            return []

    async def get_torrent_details(self, torrent_id: str) -> Optional[Dict]:
        """Fetch torrent details by TPB id."""
        try:
            resp = await self.client.get(
                f"{self.api_url}/t.php",
                params={"id": torrent_id},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("TPB details error: %s", exc)
            return None

    @staticmethod
    def build_magnet(info_hash: str, name: str) -> str:
        """Build a magnet link from info_hash and name."""
        trackers = [
            "udp://tracker.coppersurfer.tk:6969/announce",
            "udp://tracker.openbittorrent.com:80/announce",
            "udp://tracker.opentrackr.org:1337/announce",
            "udp://tracker.leechers-paradise.org:6969/announce",
        ]
        tr_params = "".join(f"&tr={quote(t)}" for t in trackers)
        return (
            f"magnet:?xt=urn:btih:{info_hash}"
            f"&dn={quote(name)}"
            f"{tr_params}"
        )

    @staticmethod
    def human_size(size_bytes: str) -> str:
        """Convert TPB size string (bytes) to human readable."""
        try:
            size = int(size_bytes)
        except (ValueError, TypeError):
            return "?"

        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(value)} B"
                return f"{value:.2f} {unit}"
            value /= 1024
        return f"{size} B"

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
