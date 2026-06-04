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

### Ubuntu One-Command Setup

On Ubuntu/Debian, you can let the setup script install system packages, create `.venv`, install Python requirements, and write `.env` interactively:

```bash
bash scripts/setup_ubuntu.sh
```

The script asks for BotFather token, Telegram `API_ID`/`API_HASH`, allowed user IDs, and whether to use automatic local ports or manually configure aria2 RPC, dashboard, and mini-app host/port settings.

### Telegram Mini-App Without a Domain

Telegram Mini Apps require a public HTTPS URL. If you do not own a domain, keep the mini-app on your VPS and expose it with a free Cloudflare Quick Tunnel:

```bash
bash scripts/start_with_cloudflare_tunnel.sh
```

The script starts `cloudflared`, writes the generated `https://....trycloudflare.com` URL to `WEB_APP_URL` in `.env`, then starts the bot. The generated URL changes when the tunnel restarts, so use this script whenever you start the bot without a domain.

## Configuration

Required environment variables:

- `BOT_TOKEN`: Telegram bot token from BotFather.
- `API_ID`: Telegram API ID from my.telegram.org.
- `API_HASH`: Telegram API hash from my.telegram.org.

Useful optional variables:

- `ALLOWED_USER_IDS`: comma-separated user IDs allowed to use the bot.
- `PYRO_SESSION_NAME`: Pyrogram session name for large file uploads.
- `ARIA2_BIN`: path to `aria2c`.
- `ARIA2_RPC_HOST`, `ARIA2_RPC_PORT`, `ARIA2_RPC_SECRET`: local aria2 JSON-RPC daemon settings. The bot starts aria2 with RPC enabled when needed and stores resume state in `Download/.aria2.session`.
- `FFMPEG_BIN`: path to `ffmpeg`.
- `TPB_API_URL`: optional API Bay mirror.
- `AUTO_CLEANUP_DAYS`: cleanup threshold for old temporary files.
- `WEB_DASHBOARD_ENABLE`: `true` or `false` to enable the local web dashboard.
- `WEB_DASHBOARD_HOST`: dashboard bind host (default `127.0.0.1`).
- `WEB_DASHBOARD_PORT`: dashboard port (default `8080`).
- `WEB_APP_ENABLE`: `true` or `false` to enable the Telegram mini-app.
- `WEB_APP_HOST`, `WEB_APP_PORT`, `WEB_APP_URL`: mini-app bind settings and public URL.
- `MINI_APP_DEFAULT_CHAT_ID`: optional private Telegram user ID fallback for mini-app settings, zip, and upload actions when Telegram does not send WebApp init data.

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
