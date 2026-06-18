# Supported Sites

Support can change when websites update their players, APIs, authentication, or anti-bot systems. Keep `yt-dlp` and project dependencies current, and use only content you are authorized to download.

## YouTube and Social Media

These providers use `yt-dlp` and support video downloads plus audio extraction where the source exposes an audio stream.

| Site | Video | Audio | Notes |
|---|---:|---:|---|
| YouTube / `youtu.be` | Yes | Yes | Public and otherwise accessible videos |
| TikTok | Yes | Yes | Public videos |
| Instagram | Yes | Yes | Public or cookie-accessible posts and reels |
| X / Twitter | Yes | Yes | Public or cookie-accessible posts |
| Facebook | Yes | Yes | Public or cookie-accessible videos |
| Vimeo | Yes | Yes | Public videos |
| Dailymotion | Yes | Yes | Public videos |
| Twitch | Yes | Yes | Content supported by the installed yt-dlp version |

Many additional sites supported natively by `yt-dlp` may work through the same downloader even when they are not listed here.

## Spotify

Spotify downloads use `spotDL`.

| Input | Supported |
|---|---:|
| Track | Yes |
| Album | Yes |
| Playlist | Yes |
| Artist | Yes |
| Show | Yes |
| Episode | Yes |

## Adult Video Providers

These routes are limited to public, non-DRM pages. Some use direct yt-dlp extractors while others resolve a media URL before download.

| Site | Status | Notes |
|---|---:|---|
| AlphaPorno | Yes | Resolved public media |
| CamSoda | Yes | Resolved public media |
| DrTuber | Yes | yt-dlp |
| Empflix | Yes | yt-dlp |
| Eporner | Yes | yt-dlp |
| HellPorno | Yes | yt-dlp |
| HQPorner | Yes | yt-dlp |
| JavHDPorn | Yes | Generic extraction with browser impersonation |
| Javtiful | Yes | Signed player media URL |
| LoveHomePorn | Yes | yt-dlp |
| MissAV | Yes | yt-dlp or resolved HLS, depending on domain |
| MissAV mirrors | Yes | `.live`, `.ws`, and `missav123.com` |
| Motherless | Yes | yt-dlp |
| NJAV | Yes | Resolved HLS |
| NonkTube | Yes | Resolved public media |
| PornHub | Yes | Individual videos and public model batches |
| PornTop | Yes | yt-dlp |
| Porntrex | Yes | yt-dlp |
| RedTube | Yes | yt-dlp |
| Rule34Video | Yes | yt-dlp |
| Sexu | Yes | Resolved public media |
| SpankBang | Yes | yt-dlp |
| SunPorno | Yes | yt-dlp |
| ThisVid | Yes | yt-dlp |
| Thothub | Yes | Generic extraction |
| TNAFlix | Yes | yt-dlp |
| Tube8 | Yes | yt-dlp |
| Txxx | Yes | yt-dlp |
| WebCamera.pl | Yes | yt-dlp |
| XHamster | Yes | yt-dlp |
| XNXX | Yes | yt-dlp |
| XVideos | Yes | yt-dlp |
| YouJizz | Yes | yt-dlp |
| YouPorn | Yes | yt-dlp |
| ZenPorn | Yes | yt-dlp |

Resolved providers use bounded filenames so temporary CDN query strings are not copied into local filenames.

## Hentai Video Providers

These routes use `yt-dlp` with `hanime-plugin==2026.5.10`. Hanime also requires Deno. The bot checks `DENO_BIN`, `PATH`, `~/.deno/bin/deno`, and `~/.local/bin/deno`.

| Site | Single Episode | Playlist / Series | Notes |
|---|---:|---:|---|
| `hstream.moe` | Yes | Yes | Series pages resolve episode URLs |
| `hentaihaven.com` | Yes | Yes | Series and studio pages resolve episode URLs |
| `hentaimama.io` | Yes | No | Single-episode route |
| `hanime.tv` | Yes | No | Requires Deno |
| `hanime.red` | Yes | No | Availability depends on the current page player |
| `ohentai.org` | No | No | Disabled after repeated access failures |
| `oppai.stream` | No | No | Disabled because the extractor currently fails |

## Manga and Galleries

| Site or Input | Supported | Notes |
|---|---:|---|
| MangaDex chapter URLs | Yes | Downloads chapter images |
| nhentai gallery URLs | Yes | Downloads gallery images |
| E-Hentai public gallery pages | Best effort | Requires discoverable public image URLs |
| Generic image galleries | Best effort | Page must expose accessible image links |

Downloaded galleries can be converted to PDF manually or automatically.

## Torrents and Direct Downloads

| Provider or Input | Supported | Notes |
|---|---:|---|
| Magnet links | Yes | aria2 download with live status |
| `.torrent` files | Yes | Includes file selection |
| Direct HTTP/HTTPS files | Yes | Resume support through aria2 |
| The Pirate Bay / API Bay | Yes | Configurable `TPB_API_URL` |
| RARBG-style mirrors | Yes | Configurable `RARBG_BASE_URL` |
| Prowlarr | Yes | Searches configured Prowlarr indexers |

## Batch Behavior

Supported playlists, series, and profiles can use either mode:

| Mode | Behavior |
|---|---|
| Upload and delete each | Download one item, upload it to Telegram, delete it after success, then continue |
| Download only | Download sequentially and retain every file on the VPS |

Batch status reports completed and remaining items, such as `3/86 done (83 remaining)`.

## Known Limitations

- DRM-protected, paid, or inaccessible private media is not supported.
- Cookies may be required for age gates, consent pages, or authenticated content.
- CAPTCHA and human-verification challenges are not bypassed.
- Provider behavior depends on the installed yt-dlp/plugin versions and can change without notice.
- Playlist, profile, channel, and model pages may require provider-specific handling even when individual videos work.
