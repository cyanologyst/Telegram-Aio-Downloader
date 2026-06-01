"""Downloader provider abstractions.

Provider modules implement this interface and are registered with
``DownloaderRegistry``. Handlers and queue services depend on the interface,
not concrete implementations, so new providers can be added without changing
Telegram-facing code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.models.download import DownloadResult


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """Input passed to a downloader provider."""

    url: str
    destination: Path
    options: Mapping[str, object] = field(default_factory=dict)


class BaseDownloader(ABC):
    """Base class for URL-based downloader providers."""

    provider_name: str

    @abstractmethod
    async def can_handle(self, url: str) -> bool:
        """Return whether this provider can download ``url``."""

    @abstractmethod
    async def download(self, request: DownloadRequest) -> DownloadResult:
        """Download content and return produced artifacts."""
