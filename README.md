# Telegram Downloader Bot

Async Telegram downloader bot for torrents, yt-dlp-supported video links, archive creation, and Telegram file upload workflows.

## Features

- Magnet and `.torrent` downloads through `aria2c`
- The Pirate Bay search integration
- yt-dlp video/audio downloads
- Telegram Bot API interaction with Pyrogram user-session uploads for large files
- File browser, batch upload/delete, archive creation, password-protected archives
- Per-user settings, including forwarded-post auto-download control

## Project Status

The project has been reorganized into a public-repository-ready package layout. The Telegram runtime is preserved in `app.bot.telegram_bot` while reusable services and new provider interfaces are extracted under `app/`.

## Quick Start

1. Install Python 3.11+.
2. Install external binaries: `aria2c` and `ffmpeg`.
3. Create a virtual environment.
4. Install dependencies:

```bash
pip install -r requirements/dev.txt
```

5. Copy `.env.example` to `.env` and fill in Telegram credentials.
6. Run:

```bash
python main.py
```

## Configuration

Required environment variables:

- `BOT_TOKEN`: Telegram bot token from BotFather.
- `API_ID`: Telegram API ID from my.telegram.org.
- `API_HASH`: Telegram API hash from my.telegram.org.

Useful optional variables:

- `ALLOWED_USER_IDS`: comma-separated user IDs allowed to use the bot.
- `PYRO_SESSION_NAME`: Pyrogram session name for large file uploads.
- `ARIA2_BIN`: path to `aria2c`.
- `FFMPEG_BIN`: path to `ffmpeg`.
- `TPB_API_URL`: optional API Bay mirror.
- `AUTO_CLEANUP_DAYS`: cleanup threshold for old temporary files.
- `WEB_DASHBOARD_ENABLE`: `true` or `false` to enable the local web dashboard.
- `WEB_DASHBOARD_HOST`: dashboard bind host (default `127.0.0.1`).
- `WEB_DASHBOARD_PORT`: dashboard port (default `8080`).

## Development

```bash
pip install -r requirements/dev.txt
ruff check app tests
black --check app tests
isort --check-only app tests
mypy app
pytest
```

## Architecture

See [docs/architecture.md](docs/architecture.md) and [docs/extension-guide.md](docs/extension-guide.md).

## License

License is intentionally left as `TBD` until the maintainer chooses one.

