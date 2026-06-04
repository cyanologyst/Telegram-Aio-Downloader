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

# Optional: Dashboard
WEB_DASHBOARD_ENABLE=false
```

## 3. No Domain: Use Cloudflare Quick Tunnel

```bash
bash scripts/start_with_cloudflare_tunnel.sh
```

This keeps Flask running on your own VPS, exposes it through a temporary HTTPS `trycloudflare.com` URL, updates `WEB_APP_URL`, and starts the bot. Run `/start` in Telegram after startup to refresh the reply keyboard button.

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
