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

`app.bot.telegram_bot` wires everything together: handlers, job bookkeeping and the download providers. It is still the largest module and the migration boundary; new behaviour belongs in the modules below.

### Telegram UI

| Module | Role |
|---|---|
| `app/bot/views/` | Pure functions that return `(html_text, keyboard)` for each screen: `home`, `settings`, `files`, `jobs`. Testable without Telegram. |
| `app/bot/search.py` | The unified torrent search flow (one message: source → query → results → details). |
| `app/bot/callbacks.py` | `answer_once` / `auto_answer`: every button press is answered exactly once. |

Conventions:

- Every update passes an access gate (`TypeHandler`, group -1) before any handler runs.
- Updates are handled concurrently; long work (uploads, conversions, zips, downloads) runs in tracked background tasks (`run_in_background`).
- Inline screens edit the message in place and always offer a way back to 🏠 Menu.
- Each download has one **job card** that is edited while it runs and replaced by a final card when it ends.
- Callback data carries short ids (path tokens, request ids, search ids), never user text, to stay under Telegram's 64-byte limit.

### Services

| Module | Role |
|---|---|
| `app/services/torrent_search.py` | Prowlarr, TPB and RARBG behind one provider interface |
| `app/services/gallery.py` | gallery-dl routing and subprocess downloads |
| `app/services/organize.py` | Plan/apply "Organize videos" safely |
| `app/services/job_store.py` | SQLite persistence of the job list |
| `app/services/archive.py` | ZIP/7Z creation and volume splitting |

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
- `zip_settings/` (per-user settings)
- `data/bot.sqlite3` (saved job list; override with `JOB_DB_PATH`)
- Pyrogram `*.session` files

## Next Refactor Targets

- Extract `Aria2DownloadService` from `app.bot.telegram_bot`.
- Extract `YtDlpDownloadService`.
- Extract `TelegramUploadService`.
- Move the job dictionaries behind a job service that owns persistence and cards.

