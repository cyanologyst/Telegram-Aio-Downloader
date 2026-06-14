"""Async crawler for RARBG-style clone pages.

The original RARBG shut down in 2023. This crawler targets public clone pages
that expose ordinary HTML and magnet links. It does not attempt to bypass
CAPTCHA, Cloudflare, or human verification pages.
"""

import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


class RARBGVerificationError(RuntimeError):
    """Raised when a RARBG-style site requires human verification."""


@dataclass(slots=True)
class RARBGResult:
    id: str
    name: str
    url: str
    category: str = "?"
    added: str = "?"
    size: str = "?"
    seeders: str = "?"
    leechers: str = "?"
    uploader: str = "?"
    magnet: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "category": self.category,
            "added": self.added,
            "size": self.size,
            "seeders": self.seeders,
            "leechers": self.leechers,
            "uploader": self.uploader,
            "magnet": self.magnet,
        }


class RARBGCrawler:
    """Search and fetch torrents from RARBG-style clone HTML pages."""

    DEFAULT_BASE_URL = "https://rargb.to"

    CATEGORIES = {
        "all": "",
        "movies": "movies",
        "tv": "tv",
        "games": "games",
        "music": "music",
        "anime": "anime",
        "apps": "apps",
        "doc": "documentaries",
        "other": "other",
        "xxx": "xxx",
    }

    CATEGORY_LABELS = {
        "all": "🌐 All",
        "movies": "🎬 Movies",
        "tv": "📺 TV",
        "games": "🎮 Games",
        "music": "🎵 Music",
        "anime": "🎞 Anime",
        "apps": "🖥 Apps",
        "doc": "📚 Doc",
        "other": "📦 Other",
        "xxx": "🔞 XXX",
    }

    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or os.getenv("RARBG_BASE_URL", self.DEFAULT_BASE_URL)).rstrip("/")
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0 Safari/537.36"
                    ),
                },
            )
        return self._client

    async def search(self, query: str, category: str = "", page: int = 0) -> list[dict]:
        """Search a RARBG-style clone and return torrent dictionaries."""
        params: dict[str, list[str] | str] = {"search": query}
        if category:
            params["category[]"] = category
        path = "/search/" if page <= 0 else f"/search/{page + 1}/"

        html = await self._get_html(path, params=params)
        return [result.to_dict() for result in self._parse_results(html)]

    async def get_torrent_details(self, torrent_id: str) -> dict | None:
        """Fetch torrent details by encoded RARBG-style path/id."""
        try:
            html = await self._get_html(self._id_to_path(torrent_id))
            detail = self._parse_detail(html, torrent_id)
            return detail.to_dict() if detail else None
        except RARBGVerificationError:
            raise
        except Exception as exc:
            logger.error("RARBG details error: %s", exc)
            return None

    async def _get_html(self, path: str, params: dict | None = None) -> str:
        try:
            resp = await self.client.get(
                urljoin(self.base_url + "/", path.lstrip("/")), params=params
            )
            resp.raise_for_status()
            html = resp.text
            self._raise_if_verification(html, str(resp.url))
            return html
        except RARBGVerificationError:
            raise
        except Exception as exc:
            logger.error("RARBG request error: %s", exc)
            return ""

    def _parse_results(self, html: str) -> list[RARBGResult]:
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        results: list[RARBGResult] = []
        seen: set[str] = set()

        for row in soup.select("tr.lista2"):
            cells = row.find_all("td")
            if len(cells) < 7:
                continue
            link = row.select_one('td.lista a[href^="/torrent/"]')
            if not link:
                continue

            href = link.get("href", "")
            torrent_id = self._path_to_id(href)
            if not torrent_id or torrent_id in seen:
                continue
            seen.add(torrent_id)

            category = " ".join(
                a.get_text(" ", strip=True) for a in cells[2].find_all("a")
            ) or cells[2].get_text(" ", strip=True)
            result = RARBGResult(
                id=torrent_id,
                name=link.get("title") or link.get_text(" ", strip=True),
                url=urljoin(self.base_url + "/", href),
                category=category or "?",
                added=cells[3].get_text(" ", strip=True),
                size=cells[4].get_text(" ", strip=True),
                seeders=cells[5].get_text(" ", strip=True),
                leechers=cells[6].get_text(" ", strip=True),
                uploader=cells[7].get_text(" ", strip=True) if len(cells) > 7 else "?",
            )
            results.append(result)

        return results

    def _parse_detail(self, html: str, torrent_id: str) -> RARBGResult | None:
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        magnet_link = soup.select_one('a[href^="magnet:"]')
        title = soup.select_one("h1.black")
        name = title.get_text(" ", strip=True) if title else "Unknown"

        detail = RARBGResult(
            id=torrent_id,
            name=name,
            url=urljoin(self.base_url + "/", self._id_to_path(torrent_id)),
            magnet=magnet_link.get("href", "") if magnet_link else "",
        )

        for row in soup.select("table.lista tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            key = cells[0].get_text(" ", strip=True).lower().rstrip(":")
            value = cells[1].get_text(" ", strip=True)
            if key == "size":
                detail.size = value
            elif key == "added":
                detail.added = value
            elif key == "category":
                detail.category = value
            elif key == "peers":
                match = re.search(r"Seeders\s*:\s*(\d+)\s*,\s*Leechers\s*:\s*(\d+)", value, re.I)
                if match:
                    detail.seeders, detail.leechers = match.groups()

        return detail

    @staticmethod
    def _path_to_id(path: str) -> str:
        return path.strip("/")

    @staticmethod
    def _id_to_path(torrent_id: str) -> str:
        return "/" + torrent_id.strip("/")

    @staticmethod
    def _raise_if_verification(html: str, url: str) -> None:
        lowered = html.lower()
        verification_terms = (
            "captcha",
            "human verification",
            "verify you are human",
            "checking your browser",
            "cf-chl",
            "challenge-platform",
        )
        has_torrent_content = (
            'href="/torrent/' in lowered or "href='magnet:" in lowered or 'href="magnet:' in lowered
        )
        if any(term in lowered for term in verification_terms) and not has_torrent_content:
            raise RARBGVerificationError(
                f"{url} requires human verification. Try a different RARBG_BASE_URL mirror."
            )

    @staticmethod
    def human_size(size: str) -> str:
        return size or "?"

    @staticmethod
    def safe_query(query: str) -> str:
        return quote(query[:60], safe="")

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None
