"""Client for a self-hosted cobalt API (https://github.com/imputnet/cobalt).

cobalt handles social sites (X/Twitter, Instagram, TikTok, Reddit, YouTube,
...) with its own extractors, so it is a second chance when yt-dlp fails.
Public instances such as api.cobalt.tools are bot-protected and not meant
for other apps; point COBALT_API_URL at your own instance.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ERROR_TEXT = {
    "error.api.link.invalid": "cobalt couldn't read this link.",
    "error.api.link.unsupported": "cobalt doesn't support this link.",
    "error.api.service.unsupported": "cobalt doesn't support this site.",
    "error.api.service.disabled": "This site is turned off on your cobalt instance.",
    "error.api.content.video.unavailable": "This video is unavailable.",
    "error.api.content.video.live": "Live streams can't be downloaded.",
    "error.api.content.video.private": "This video is private.",
    "error.api.content.video.age": "This video is age-restricted.",
    "error.api.content.video.region": "This video is blocked in the server's region.",
    "error.api.content.too_long": "This is longer than your cobalt instance allows.",
    "error.api.content.post.unavailable": "This post is unavailable.",
    "error.api.content.post.private": "This post is private.",
    "error.api.content.post.age": "This post is age-restricted.",
    "error.api.youtube.login": "YouTube asked cobalt to log in (it's blocking the server).",
    "error.api.youtube.token_expired": "cobalt's YouTube session expired.",
    "error.api.fetch.fail": "cobalt couldn't get the media from the site.",
    "error.api.fetch.critical": "cobalt couldn't get the media from the site.",
    "error.api.fetch.empty": "The site gave cobalt nothing to download.",
    "error.api.fetch.rate": "The site is rate-limiting cobalt; try again later.",
    "error.api.rate_exceeded": "Too many requests to cobalt; try again in a minute.",
    "error.api.auth.key.missing": "cobalt wants an API key; set COBALT_API_KEY.",
    "error.api.auth.key.invalid": "cobalt rejected the API key in COBALT_API_KEY.",
    "error.api.auth.jwt.missing": "This cobalt instance is bot-protected; use your own instance.",
}

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
AUDIO_EXTS = {".mp3", ".ogg", ".opus", ".wav", ".m4a", ".flac"}
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}


class CobaltError(RuntimeError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or ERROR_TEXT.get(code) or f"cobalt error: {code}")


@dataclass(frozen=True)
class CobaltItem:
    url: str
    filename: str
    kind: str  # video / audio / photo / gif


def kind_from_name(filename: str, default: str = "video") -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".gif":
        return "gif"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in VIDEO_EXTS:
        return "video"
    return default


def safe_filename(name: str, fallback: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .")
    return name[:180] or fallback


def parse_response(data: dict[str, Any]) -> list[CobaltItem]:
    """Turn a POST / response into files to fetch (or raise CobaltError)."""
    status = data.get("status")
    if status in ("tunnel", "redirect"):
        filename = safe_filename(str(data.get("filename") or ""), "cobalt.mp4")
        return [CobaltItem(str(data["url"]), filename, kind_from_name(filename))]
    if status == "picker":
        items = []
        for index, entry in enumerate(data.get("picker") or [], start=1):
            kind = str(entry.get("type") or "photo")
            ext = {"photo": ".jpg", "gif": ".mp4", "video": ".mp4"}.get(kind, ".bin")
            items.append(CobaltItem(str(entry["url"]), f"item_{index:02d}{ext}", kind))
        if data.get("audio"):
            name = safe_filename(str(data.get("audioFilename") or ""), "audio.mp3")
            items.append(CobaltItem(str(data["audio"]), name, "audio"))
        if not items:
            raise CobaltError("error.api.fetch.empty")
        return items
    if status == "error":
        error = data.get("error") or {}
        raise CobaltError(str(error.get("code") or "error.unknown"))
    raise CobaltError("error.unexpected", f"cobalt answered with an unexpected status: {status}")


def _free_path(destination: Path, filename: str) -> Path:
    """``destination/filename``, numbered when that name is taken."""
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / filename
    stem, suffix, n = target.stem, target.suffix, 1
    while target.exists():
        n += 1
        target = destination / f"{stem} ({n}){suffix}"
    return target


class CobaltClient:
    def __init__(
        self,
        api_url: str | None,
        api_key: str | None = None,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,  # tests
    ):
        self.api_url = (api_url or "").strip()
        if self.api_url and not self.api_url.endswith("/"):
            self.api_url += "/"
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.transport = transport
        self._services: set[str] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_url)

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Api-Key {self.api_key}"
        return headers

    async def services(self) -> set[str]:
        """Sites the instance supports (GET /), cached after the first call."""
        if self._services is None:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.get(self.api_url, headers={"Accept": "application/json"})
                response.raise_for_status()
                self._services = set(response.json().get("cobalt", {}).get("services", []))
        return self._services

    async def resolve(
        self,
        url: str,
        *,
        audio_only: bool = False,
        mute: bool = False,
        max_height: int | None = None,
    ) -> list[CobaltItem]:
        body = {
            "url": url,
            "videoQuality": str(max_height) if max_height else "max",
            "downloadMode": "audio" if audio_only else "mute" if mute else "auto",
            "audioFormat": "mp3",
            "filenameStyle": "pretty",
            "convertGif": False,  # Telegram shows silent MP4s as GIFs, and they're smaller
        }
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            try:
                response = await client.post(self.api_url, json=body, headers=self._headers())
            except httpx.HTTPError as exc:
                raise CobaltError(
                    "error.connect", f"Can't reach cobalt at {self.api_url}: {exc}"
                ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise CobaltError(
                "error.unexpected", f"cobalt answered HTTP {response.status_code}"
            ) from exc
        return parse_response(data)

    async def download(
        self,
        item: CobaltItem,
        destination: Path,
        *,
        progress: Callable[[int, int], None] | None = None,
        is_cancelled: Callable[[], bool] = lambda: False,
    ) -> Path:
        target = _free_path(destination, item.filename)
        partial = target.with_name(target.name + ".part")
        done = 0
        timeout = httpx.Timeout(self.timeout, read=120)
        try:
            async with (
                httpx.AsyncClient(
                    timeout=timeout, follow_redirects=True, transport=self.transport
                ) as client,
                client.stream("GET", item.url) as response,
            ):
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                if not total:  # cobalt tunnels announce the size here
                    total = int(response.headers.get("estimated-content-length") or 0)
                with partial.open("wb") as fh:
                    async for chunk in response.aiter_bytes(256 * 1024):
                        if is_cancelled():
                            raise CobaltError("cancelled", "Cancelled by user")
                        fh.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
        except httpx.HTTPError as exc:
            partial.unlink(missing_ok=True)
            raise CobaltError("error.api.fetch.fail", f"cobalt download failed: {exc}") from exc
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        if done == 0:
            partial.unlink(missing_ok=True)
            raise CobaltError(
                "error.api.fetch.empty",
                "cobalt sent an empty file (the site probably refused the server).",
            )
        partial.rename(target)
        return target
