"""Async crawler for RARBG-style clone pages.

The original RARBG shut down in 2023. This crawler targets public clone pages
that expose ordinary HTML and magnet links. It does not attempt to bypass
CAPTCHA, Cloudflare, or human verification pages.

Two layouts are supported:

- ``therarbg.to`` style pages (default): ``tr.list-entry`` result rows,
  ``/post-detail/<pk>/<slug>/`` detail links, ``/get-posts/...`` search URLs.
- Legacy ``rargb.to`` style pages: ``tr.lista2`` rows, ``/torrent/<id>.html``
  links, ``/search/...`` URLs. Kept for users pointing ``RARBG_BASE_URL`` at
  older mirror software.

Both sites sit behind Cloudflare TLS fingerprinting, so requests are made with
curl_cffi impersonating Chrome; plain httpx/urllib clients get 403/challenge
pages.
"""

import logging
import os
import re
from dataclasses import dataclass
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

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

    DEFAULT_BASE_URL = "https://therarbg.to"

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

    # therarbg.to spells its categories differently from our internal codes;
    # str.capitalize() would send "Tv"/"Xxx", which the site does not match.
    THERARBG_CATEGORY_NAMES = {
        "movies": "Movies",
        "tv": "TV",
        "games": "Games",
        "music": "Music",
        "anime": "Anime",
        "apps": "Apps",
        "documentaries": "Documentaries",
        "other": "Other",
        "xxx": "XXX",
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
        self._client: AsyncSession | None = None

    @property
    def client(self) -> AsyncSession:
        if self._client is None:
            self._client = AsyncSession(
                impersonate="chrome",
                timeout=30.0,
                headers={"Accept-Language": "en-US,en;q=0.9"},
            )
        return self._client

    @property
    def _is_therarbg(self) -> bool:
        return "therarbg" in self.base_url.lower()

    async def search(self, query: str, category: str = "", page: int = 0) -> list[dict]:
        """Search a RARBG-style clone and return torrent dictionaries."""
        if self._is_therarbg:
            html = await self._get_html(self._therarbg_search_path(query, category, page))
        else:
            params: dict[str, list[str] | str] = {"search": query}
            if category:
                params["category[]"] = category
            path = "/search/" if page <= 0 else f"/search/{page + 1}/"
            html = await self._get_html(path, params=params)

        if not html:
            return []
        results = self._parse_therarbg_results(html)
        if not results:
            results = self._parse_results(html)
        return [result.to_dict() for result in results]

    async def get_torrent_details(self, torrent_id: str) -> dict | None:
        """Fetch torrent details by encoded RARBG-style path/id."""
        try:
            html = await self._get_html(self._detail_path(torrent_id))
            detail = self._parse_detail(html, torrent_id)
            return detail.to_dict() if detail else None
        except RARBGVerificationError:
            raise
        except Exception as exc:
            logger.error("RARBG details error: %s", exc)
            return None

    def _detail_path(self, torrent_id: str) -> str:
        path = self._id_to_path(torrent_id)
        # therarbg.to redirects extension-less paths to a listing page unless
        # they end with a trailing slash.
        if self._is_therarbg and not path.endswith(".html") and not path.endswith("/"):
            path += "/"
        return path

    def _therarbg_search_path(self, query: str, category: str, page: int) -> str:
        segments = [f"keywords:{self.safe_query(query)}"]
        if category and category != "all":
            name = self.THERARBG_CATEGORY_NAMES.get(
                str(category).lower(), str(category).capitalize()
            )
            segments.append(f"category:{quote(name)}")
        if page > 0:
            segments.append(f"page:{page + 1}")
        return "/get-posts/" + "/".join(segments) + "/"

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

    # ------------------------------------------------------------------
    # Parsing: therarbg.to layout
    # ------------------------------------------------------------------

    def _parse_therarbg_results(self, html: str) -> list[RARBGResult]:
        soup = BeautifulSoup(html, "html.parser")
        results: list[RARBGResult] = []
        seen: set[str] = set()

        for row in soup.select("tr.list-entry"):
            cells = row.find_all("td")
            link = row.select_one('a[href^="/post-detail/"]')
            if not link:
                continue

            href = link.get("href", "")
            torrent_id = href.strip("/") if isinstance(href, str) else ""
            if not torrent_id or torrent_id in seen:
                continue
            seen.add(torrent_id)

            category_cell = cells[2] if len(cells) > 2 else None
            size_cell = row.select_one("td.sizeCell")
            colored = row.find_all("td", style=re.compile(r"color:\s*(green|red)", re.I))
            seeders = colored[0].get_text(" ", strip=True) if colored else "?"
            leechers = colored[1].get_text(" ", strip=True) if len(colored) > 1 else "?"
            added_cell = next(
                (c for c in cells if c.get("data-order") and c is not size_cell),
                None,
            )

            result = RARBGResult(
                id=torrent_id,
                name=link.get_text(" ", strip=True),
                url=urljoin(self.base_url + "/", torrent_id + "/"),
                category=(
                    category_cell.get_text(" ", strip=True) if category_cell else "?"
                )
                or "?",
                added=added_cell.get_text(" ", strip=True) if added_cell else "?",
                size=size_cell.get_text(" ", strip=True) if size_cell else "?",
                seeders=seeders or "?",
                leechers=leechers or "?",
            )
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Parsing: legacy rargb.to layout
    # ------------------------------------------------------------------

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

        title = soup.select_one("h1.black") or soup.select_one(".panel-heading h1")
        if title:
            name = title.get_text(" ", strip=True)
        else:
            title_tag = soup.find("title")
            raw = title_tag.get_text(" ", strip=True) if title_tag else ""
            # therarbg.to titles look like:
            # "Download <name>. Free Torrent from The RarBg"
            name = re.sub(
                r"^(?:Download\s+)?(.*?)\.(?:\s*Free Torrent from .*)?$",
                r"\1",
                raw,
            ) or raw
            name = name.strip() or "Unknown"

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
            'href="/torrent/' in lowered
            or 'href="/post-detail/' in lowered
            or "href='magnet:" in lowered
            or 'href="magnet:' in lowered
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
            await self._client.close()
            self._client = None
