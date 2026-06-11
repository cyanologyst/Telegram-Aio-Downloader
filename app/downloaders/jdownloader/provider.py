"""JDownloader API provider.

The provider expects a small JDownloader/MyJDownloader bridge service exposing
``POST /downloads``. Keeping this boundary HTTP-based avoids binding the bot to
one specific JDownloader deployment model.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx

from app.downloaders.base import BaseDownloader, DownloadRequest
from app.models.download import DownloadResult


class JDownloaderProvider(BaseDownloader):
    """Delegate complex hoster/container links to a JDownloader bridge."""

    provider_name = "jdownloader"
    _container_re = re.compile(r"https?://.*\.(?:dlc|ccf|rsdf)(?:[?#].*)?$", re.IGNORECASE)

    def __init__(
        self,
        api_url: str,
        api_token: str = "",
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.timeout_seconds = timeout_seconds

    async def can_handle(self, url: str) -> bool:
        if not self.api_url:
            return False
        return bool(self._container_re.search(url.strip())) or bool(url.strip().startswith("jd:"))

    async def download(self, request: DownloadRequest) -> DownloadResult:
        if not self.api_url:
            raise RuntimeError("JDOWNLOADER_API_URL is not configured")

        download_url = request.url.removeprefix("jd:")
        payload = {
            "url": download_url,
            "destination": str(request.destination),
            "options": dict(request.options),
        }
        headers = self._headers()
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.api_url}/downloads", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()

        return DownloadResult(
            provider=self.provider_name,
            title=str(data.get("title") or Path(download_url).name or "JDownloader task"),
            artifacts=(),
            metadata=self._metadata(data),
        )

    def _headers(self) -> dict[str, str]:
        if not self.api_token:
            return {}
        return {"Authorization": f"Bearer {self.api_token}"}

    @staticmethod
    def _metadata(data: dict[str, Any]) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for key in ("task_id", "package_id", "status", "message"):
            value = data.get(key)
            if value is not None:
                metadata[key] = str(value)
        return metadata
