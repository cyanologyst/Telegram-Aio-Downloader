"""Downloader provider registry."""

from __future__ import annotations

from collections.abc import Iterable

from app.downloaders.base import BaseDownloader


class DownloaderRegistry:
    """Resolve URL download requests to provider implementations."""

    def __init__(self, providers: Iterable[BaseDownloader] | None = None) -> None:
        self._providers: list[BaseDownloader] = list(providers or [])

    def register(self, provider: BaseDownloader) -> None:
        """Register a provider if it is not already present."""
        if provider not in self._providers:
            self._providers.append(provider)

    @property
    def providers(self) -> tuple[BaseDownloader, ...]:
        """Registered providers in resolution order."""
        return tuple(self._providers)

    async def resolve(self, url: str) -> BaseDownloader | None:
        """Return the first provider able to handle ``url``."""
        for provider in self._providers:
            if await provider.can_handle(url):
                return provider
        return None
