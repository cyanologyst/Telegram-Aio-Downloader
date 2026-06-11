# Extension Guide

This guide explains how to add a new download provider.

## 1. Create a Provider

Add a module under `app/downloaders/<platform>/provider.py`.

```python
from app.downloaders.base import BaseDownloader, DownloadRequest
from app.models.download import DownloadResult


class ExampleDownloader(BaseDownloader):
    provider_name = "example"

    async def can_handle(self, url: str) -> bool:
        return "example.com" in url

    async def download(self, request: DownloadRequest) -> DownloadResult:
        ...
```

## 2. Register the Provider

Register it in `app/downloaders/factory.py`:

```python
from app.downloaders.example import ExampleDownloader


def build_downloader_registry(settings: Settings) -> DownloaderRegistry:
    registry = DownloaderRegistry()
    registry.register(ExampleDownloader())
    return registry
```

The current legacy runtime is still being extracted, so provider registration
should happen in the new service layer as that migration continues.

## 3. Keep Provider Boundaries Clean

Providers should:

- Accept a `DownloadRequest`.
- Return a `DownloadResult`.
- Avoid Telegram-specific imports.
- Use timeouts and cancellation-aware async subprocess handling.
- Store provider-specific metadata in `DownloadResult.metadata`.
- Prefer wrappers around mature external tools/APIs over copying third-party
  bot code into this repository.

## 4. Testing

Add tests under `tests/downloaders`. Test `can_handle` directly and mock external network/process calls for `download`.
