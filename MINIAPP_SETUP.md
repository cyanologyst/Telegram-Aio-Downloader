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
WEB_APP_URL=http://127.0.0.1:5000

# Optional: Dashboard
WEB_DASHBOARD_ENABLE=false
```

## 3. For Local Testing with Ngrok

```bash
# Terminal 1: Start your bot
python main.py

# Terminal 2: In another terminal
ngrok http 5000

# Copy the HTTPS URL from ngrok output and update .env:
WEB_APP_URL=https://xxxxx-xxxx-xxx.ngrok.io

# Restart the bot to pick up new URL
```

## 4. Access the Mini-App

In Telegram:
- Send `/start` to your bot
- Click "Open Modern File Browser" button
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
- Solution: Make sure `WEB_APP_ENABLE=true` in `.env`

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
