# Telegram AIO Downloader Bot 🚀

An async Telegram bot and Telegram Mini App for managing downloads, files, archives, uploads, torrents, music, manga galleries, social videos, adult videos, and hentai videos from one clean control surface.

The bot is designed around a VPS workflow: send a link in Telegram, let the server download it, manage files from the chat or Mini App, then upload selected files back to your Telegram Saved Messages with a Pyrogram user session.

> ⚠️ Use this project only for content you are allowed to access and download. The bot does not bypass DRM, paid access, private content restrictions, or copyright law.

## ✨ Main Features

| Area | What It Can Do | Engine |
|---|---|---|
| 🧲 Torrents | Magnet links, `.torrent` files, metadata resolution, resume, pause, cancel, speed/ETA tracking | `aria2c` daemon |
| 🌐 Direct links | Direct HTTP/HTTPS file downloads with aria2 resume and live status | `aria2c` daemon |
| 🎬 Video sites | Download video or MP3 from supported `yt-dlp` sites | `yt-dlp` |
| 🔞 Adult video sites | Download supported public adult video pages into a separate folder | `yt-dlp` |
| 🎞️ Hentai video sites | Download supported hentai episodes and selected site playlists | `yt-dlp` + `hanime-plugin` |
| 🎵 Spotify | Download Spotify tracks, albums, playlists, artists, shows, and episodes | `spotDL` |
| 🖼️ Manga/gallery | Download image galleries and optionally convert them to PDF | gallery scraper + Pillow |
| 📦 Archives | Create ZIP/TAR/7Z archives, split large archives, optional passwords | archive services |
| 📤 Telegram upload | Upload selected files/folders/archives to Saved Messages or your account target | Pyrogram |
| 📱 Mini App | Neumorphic file manager, downloads tab, storage tab, settings tab, upload/delete/zip actions | Flask + Telegram Web App |
| 🏴‍☠️ TPB search | Search The Pirate Bay/API Bay mirrors and start magnet downloads | TPB crawler |

## 📥 Supported Input Types

| Input You Send | Result | Default Folder |
|---|---|---|
| `magnet:?xt=...` | Torrent download with live aria2 status | `Download/` |
| `.torrent` file | Torrent download | `Download/` |
| Direct file URL | Server-side HTTP/HTTPS download | `Download/` |
| Spotify URL | Audio download via spotDL | `Download/Spotify/` |
| Manga/gallery URL | Gallery image download | `Download/Manga/<gallery>/` |
| Supported video URL | Video/MP3 prompt, then `yt-dlp` download | `Download/` |
| Adult video URL | Video/MP3 prompt, separated by site | `Download/Adult/<site>/` |
| Hentai episode URL | Video/MP3 prompt, separated by site | `Download/Hentai/<site>/` |
| Hentai playlist/series URL | Download all detected episodes | `Download/Hentai/<site>/` |

## 🌍 Website Support Matrix

### General Video / Social Sites

These are routed through `yt-dlp`. Support depends on the installed `yt-dlp` version and the site’s current extractor status.

| Site | Video | MP3 | Notes |
|---|---:|---:|---|
| YouTube / `youtu.be` | ✅ | ✅ | Videos and audio extraction |
| TikTok | ✅ | ✅ | Public videos |
| Instagram | ✅ | ✅ | Public/reachable posts and reels |
| X / Twitter | ✅ | ✅ | Public/reachable posts |
| Facebook | ✅ | ✅ | Public/reachable videos |
| Vimeo | ✅ | ✅ | Public videos |
| Dailymotion | ✅ | ✅ | Public videos |
| Twitch | ✅ | ✅ | Public videos supported by `yt-dlp` |

### Adult Video Sites

These are routed into `Download/Adult/<site>/`.

| Site | Status | Notes |
|---|---:|---|
| PornHub | ✅ | Public video pages |
| Eporner | ✅ | Public video pages |
| XVideos | ✅ | Public video pages |
| XHamster | ✅ | Public video pages |
| XNXX | ✅ | Public video pages |
| SpankBang | ✅ | Public video pages |
| MissAV | ✅ | Public video pages where `yt-dlp` supports them |
| YouPorn | ✅ | Public video pages |
| Porntrex | ✅ | Public video pages |
| HQPorner | ✅ | Public video pages |
| RedTube | ✅ | Public video pages |
| Tube8 | ✅ | Public video pages |
| TNAFlix | ✅ | Public video pages |
| DrTuber | ✅ | Public video pages |
| Motherless | ✅ | Public video pages |
| ThisVid | ✅ | Public video pages |
| Rule34Video | ✅ | Public video pages |
| Txxx | ✅ | Public video pages |
| SunPorno | ✅ | Public video pages |
| YouJizz | ✅ | Public video pages |
| Empflix | ✅ | Public video pages |

### Hentai Video Sites

These require `hanime-plugin==2026.5.10` and are routed into `Download/Hentai/<site>/`.

| Site | Single Episode | Playlist / Series | Tested Behavior |
|---|---:|---:|---|
| `hstream.moe` | ✅ | ✅ | Series pages are scraped for episode URLs, then each episode downloads with `yt-dlp` |
| `hentaihaven.com` | ✅ | ✅ | Series pages are scraped for `/episode-N` URLs |
| `hentaimama.io` | ✅ | ❌ | Tested as single episode only; no stable playlist page shape wired yet |
| `hanime.red` | ✅ | ❌ | Some pages return direct MP4 URLs; treated as single episode and marked more brittle |
| `hanime.tv` | ❌ | ❌ | Disabled: requires Deno and previously failed server/CDN checks |
| `ohentai.org` | ❌ | ❌ | Disabled: returned 403/timeout during local tests |
| `oppai.stream` | ❌ | ❌ | Disabled: plugin extractor currently fails request handling |

## 📊 Live Download Management

### Torrent Search

| Provider | Search | Instant Download | Notes |
|---|---:|---:|---|
| The Pirate Bay / API Bay | ✅ | ✅ | Uses the configurable `TPB_API_URL` JSON API |
| RARBG-style mirrors | ✅ | ✅ | Defaults to `https://rargb.to`; refuses CAPTCHA/human-verification pages instead of bypassing them |

| Download Type | Live Progress | Speed | Pause/Resume | Cancel |
|---|---:|---:|---:|---:|
| aria2 torrents | ✅ | ✅ down/up | ✅ | ✅ |
| aria2 direct links | ✅ | ✅ down | ✅ | ✅ |
| yt-dlp videos | ✅ | ✅ down | ❌ | ✅ best-effort |
| Spotify | ✅ limited | ✅ when available | ❌ | ✅ |
| Manga/gallery | ✅ stage-based | ❌ | ❌ | ✅ |
| Hentai playlists | ✅ episode count + child jobs | ✅ per episode | ❌ | ✅ best-effort |

`yt-dlp` progress depends on what the extractor reports. Some sites provide exact file size and percentage; others only provide downloaded bytes and speed until the file finishes.

## 📱 Telegram Mini App

The Mini App gives you a phone-friendly file manager and download dashboard.

| Tab | Features |
|---|---|
| 📁 Files | Browse folders, persistent multi-select, selected-size counter, upload selected files, delete, zip |
| ⬇️ Downloads | Paste URLs/magnets, start downloads, view active/recent jobs, cancel supported jobs |
| 💽 Space | Storage summary, usage breakdown, cleanup actions |
| ⚙️ Settings | Upload destination, manga PDF settings, archive defaults, mini-app preferences |

Telegram Mini Apps require HTTPS. If you do not own a domain, use the included Cloudflare Quick Tunnel script.

## 📦 Archive / ZIP Features

| Feature | Support |
|---|---:|
| ZIP archives | ✅ |
| TAR archives | ✅ |
| 7Z archives | ✅ |
| Password-protected ZIP | ✅ |
| Split large archives | ✅ |
| Zip selected files across folders | ✅ |
| Auto-upload archive after zip | ✅ from Mini App |
| Auto-delete source files after zip | ✅ configurable |

## 🖼️ Manga / Gallery Features

| Feature | Support |
|---|---:|
| Download gallery images | ✅ |
| Save each gallery in its own folder | ✅ |
| Manual “Convert to PDF” from file browser | ✅ |
| Auto-convert downloaded manga to PDF | ✅ setting |
| Remove source images after PDF conversion | ✅ setting |
| Put generated PDFs in parent `Download/` folder | ✅ |

## 🎵 Spotify Features

Spotify links are handled through `spotDL`.

| Spotify Link Type | Support |
|---|---:|
| Track | ✅ |
| Album | ✅ |
| Playlist | ✅ |
| Artist | ✅ |
| Episode | ✅ |
| Show | ✅ |

Spotify files are saved under `Download/Spotify/`.

## 🛠️ Quick Start

### Ubuntu One-Command Setup

```bash
bash scripts/setup_ubuntu.sh
```

The setup script can:

| Step | What It Does |
|---|---|
| 1 | Creates/activates `.venv` |
| 2 | Installs Python requirements |
| 3 | Installs system dependencies like `aria2c`, `ffmpeg`, and helper packages |
| 4 | Asks for BotFather token, `API_ID`, `API_HASH`, allowed user IDs |
| 5 | Lets you choose automatic or manual ports/IP settings |

### Start With Mini App Tunnel

```bash
bash scripts/start_with_cloudflare_tunnel.sh
```

This starts a Cloudflare Quick Tunnel, writes the generated HTTPS URL to `.env`, and starts the bot. Use this every time you restart the Mini App without a permanent domain.

### Manual Development Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements/dev.txt
python main.py
```

## ⚙️ Configuration

Required environment variables:

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from BotFather |
| `API_ID` | Telegram API ID from `my.telegram.org` |
| `API_HASH` | Telegram API hash from `my.telegram.org` |

Useful optional variables:

| Variable | Description |
|---|---|
| `ALLOWED_USER_IDS` | Comma-separated Telegram user IDs allowed to use the bot |
| `PYRO_SESSION_NAME` | Pyrogram session name for uploads |
| `ARIA2_BIN` | Path to `aria2c` |
| `ARIA2_RPC_HOST` / `ARIA2_RPC_PORT` / `ARIA2_RPC_SECRET` | aria2 daemon RPC settings |
| `FFMPEG_BIN` | Path to `ffmpeg` |
| `SPOTDL_BIN` | Path to `spotdl` |
| `YTDLP_COOKIES_FILE` | Optional Netscape cookies file for sites needing login/consent |
| `YTDLP_PROXY` | Optional proxy URL for `yt-dlp` |
| `TPB_API_URL` | Optional API Bay mirror |
| `RARBG_BASE_URL` | Optional RARBG-style mirror base URL; useful when the default mirror challenges your VPS |
| `AUTO_CLEANUP_DAYS` | Cleanup threshold for old temporary files |
| `WEB_DASHBOARD_ENABLE` | Enable local web dashboard |
| `WEB_DASHBOARD_HOST` / `WEB_DASHBOARD_PORT` | Dashboard bind settings |
| `WEB_APP_ENABLE` | Enable Telegram Mini App |
| `WEB_APP_HOST` / `WEB_APP_PORT` / `WEB_APP_URL` | Mini App bind and public URL |
| `MINI_APP_DEFAULT_CHAT_ID` | Fallback user/chat ID for Mini App actions when Telegram init data is missing |

## 🧪 Development Checks

```bash
ruff check app tests
black --check app tests
isort --check-only app tests
mypy app
pytest
```

## 🧭 Architecture

The legacy Telegram runtime still lives in `app.bot.telegram_bot`, while reusable logic is split into services and downloader/provider modules under `app/`.

Helpful docs:

- [Architecture](docs/architecture.md)
- [Extension Guide](docs/extension-guide.md)
- [Mini App Guide](docs/miniapp-guide.md)

## 📄 License

License is intentionally left as `TBD` until the maintainer chooses one.
