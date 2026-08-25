# Mini-App Setup Quick Start

## 1. Install Dependencies

```bash
pip install Flask>=2.3.0 flask-cors>=4.0.0
```

Or install all requirements:
```bash
pip install -r requirements.txt
```

## 2. Configure Environment

Create/edit `.env` file in the project root:

```env
# Telegram Bot
BOT_TOKEN=your_bot_token
API_ID=your_api_id
API_HASH=your_api_hash

# Mini-App Settings
WEB_APP_ENABLE=true
WEB_APP_HOST=127.0.0.1
WEB_APP_PORT=5000
# Telegram requires a public HTTPS URL here.
WEB_APP_URL=https://your-public-mini-app-url
# Require a signed Telegram initData header on every API call. Keep this on.
WEB_APP_REQUIRE_AUTH=true

# Optional: Dashboard
WEB_DASHBOARD_ENABLE=false
```

## 3. No Domain, No SSL: Cloudflare Quick Tunnel (Windows/macOS/Linux)

```bash
python scripts/start_with_tunnel.py
```

Requires `cloudflared` on PATH (or set `CLOUDFLARED_BIN`). This starts the bot, exposes Flask through a temporary HTTPS `trycloudflare.com` URL, updates `WEB_APP_URL`, and configures the Mini App button automatically. Send `/start` in Telegram after startup to refresh the button.

Linux users can alternatively use `bash scripts/start_with_cloudflare_tunnel.sh`.

Over plain HTTP (`WEB_APP_URL=http://...`), Telegram refuses native Mini App buttons; the bot then shows a browser button instead so the dashboard still opens from LAN/desktop without any SSL setup.

Cloudflare Quick Tunnel URLs change each time the tunnel restarts. If you later buy a domain, point it to a named Cloudflare Tunnel for a stable URL.

## 4. Access the Mini-App

In Telegram:
- Send `/start` to your bot
- Click the "Mini-App" button in the reply keyboard
- Or use `/browse` command

## Features Available

✅ Browse files and folders  
✅ Search by name or file type  
✅ Batch delete files  
✅ Create ZIP/7Z archives  
✅ Upload to Telegram  
✅ Download from URL  
✅ Real-time statistics  
✅ Grid/List view toggle  

## Troubleshooting

**Issue**: Button doesn't appear in Telegram
- Solution: Make sure `WEB_APP_ENABLE=true` and `WEB_APP_URL` starts with `https://`, then restart and send `/start`

**Issue**: "Cannot load app" error
- Solution: Check your `WEB_APP_URL` - must be HTTPS for production

**Issue**: Files not showing up
- Solution: Verify `DOWNLOAD_DIR` has files and bot has read permission

**Issue**: API returns `401 Unauthorized`
- Cause: The request carried no valid Telegram `initData` signature. This is expected when you open `WEB_APP_URL` directly in a desktop browser - the signature only exists inside Telegram.
- Solution: Open the Mini App from your bot chat. For local debugging only, set `WEB_APP_REQUIRE_AUTH=false` and bind to `127.0.0.1`.

**Issue**: API returns `403 Forbidden`
- Cause: Your Telegram user ID is not in `ALLOWED_USER_IDS`.
- Solution: Add your ID, or clear `ALLOWED_USER_IDS` to allow any signed user.

## Security

The mini-app API can list, download and delete everything under `DOWNLOAD_DIR`, and can start uploads to Telegram. It is protected by two things:

- `WEB_APP_REQUIRE_AUTH=true` (default) rejects any request without a valid, non-stale Telegram `initData` signature. Signatures older than 24 hours are refused, so a captured header cannot be replayed indefinitely.
- CORS is scoped to `WEB_APP_URL`, so other websites cannot read API responses from your browser.

With auth disabled, anything that can reach `WEB_APP_HOST:WEB_APP_PORT` has full control over your files - never combine `WEB_APP_REQUIRE_AUTH=false` with a tunnel or a public bind address.

Uploads always go to the signed-in user's own chat; a `chat_id` in the request body is ignored while auth is enforced.

## Architecture

```
┌─────────────────────┐
│  Telegram Client    │
└──────────┬──────────┘
           │ (WebApp Button Click)
           ▼
┌─────────────────────┐         ┌──────────────────┐
│   Mini-App UI       │◄──────►│   Flask Backend  │
│  (HTML/CSS/JS)      │ (HTTPS) │   API Endpoints  │
└─────────────────────┘         └──────────┬───────┘
                                           │
                                           ▼
                                 ┌──────────────────┐
                                 │  Download Folder │
                                 │  (File System)   │
                                 └──────────────────┘
```

## Next Steps

1. Test locally with `python main.py`
2. Use ngrok for testing from your phone
3. Set up a VPS with HTTPS for production
4. Configure with your domain/IP

For detailed documentation, see [miniapp-guide.md](miniapp-guide.md)
