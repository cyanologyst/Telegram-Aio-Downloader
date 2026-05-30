# Architecture Overview

The bot is now organized around a package-first architecture.

```text
app/
  bot/                 Telegram application wiring and legacy runtime
  config/              Typed environment settings
  downloaders/         Provider interfaces and concrete providers
  handlers/            Telegram handler modules
  infrastructure/      External process/client adapters
  models/              Domain dataclasses and enums
  services/            Business services and reusable logic
  tasks/               Background task helpers
  utils/               Shared utility helpers
```

## Current Runtime

`app.bot.telegram_bot` preserves the existing production behavior. It is still the largest module and should be treated as the migration boundary: new code should not be added there unless it is wiring extracted services together.

## Extraction Strategy

1. Keep user-visible behavior stable.
2. Move reusable logic into `app/services`.
3. Move provider-specific behavior into `app/downloaders`.
4. Keep Telegram handlers thin and dependent on services/interfaces.
5. Replace global dictionaries with injected task/queue services over time.

## Downloader Model

Downloader providers implement:

```python
class BaseDownloader:
    async def can_handle(self, url: str) -> bool: ...
    async def download(self, request: DownloadRequest) -> DownloadResult: ...
```

The `DownloaderRegistry` resolves URLs to providers. This allows future sources to be added without editing core dispatch logic.

## Operational Data

Runtime data is intentionally ignored by git:

- `Download/`
- `logs/`
- `zip_settings/`
- Pyrogram `*.session` files

## Next Refactor Targets

- Extract `Aria2DownloadService` from `app.bot.telegram_bot`.
- Extract `YtDlpDownloadService`.
- Extract `TelegramUploadService`.
- Replace in-memory job dictionaries with a queue service abstraction.
- Add a persistence adapter for resumable jobs.

