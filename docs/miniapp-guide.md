# Modern File Browser Mini-App

A beautiful, professional Telegram mini-app for managing downloaded files with advanced features like batch operations, archiving, and real-time statistics.

## Features

### File Management
- 📁 **Browse Files** - Navigate through your download directory with a modern interface
- 🔍 **Search & Filter** - Find files by name or type (videos, audio, images, archives)
- 📊 **Grid/List View** - Toggle between grid and list view modes
- 🎬 **Thumbnails** - Image previews in the file browser

### Batch Operations
- ☑️ **Select All** - Quickly select all files in the current directory
- ☐ **Clear Selection** - Deselect all files
- 🗑️ **Batch Delete** - Delete multiple files at once with confirmation
- 📦 **Create Archives** - Create ZIP or 7Z archives from selected files

### Advanced Features
- 📤 **Upload to Telegram** - Upload files to Saved Messages or channels
- 📥 **Download Files** - Download new files using URLs or magnet links
- 📊 **Statistics** - Real-time storage usage and file count statistics
- 🌐 **Modern UI** - Beautiful, responsive design that works on all devices

## Installation

### 1. Update Dependencies

The Flask web framework and supporting libraries are required:

```bash
pip install -r requirements.txt
```

Or update the base requirements:

```bash
pip install Flask>=2.3.0 flask-cors>=4.0.0
```

### 2. Environment Configuration

Add these environment variables to your `.env` file:

```env
# Mini-App Server Configuration
WEB_APP_ENABLE=true                    # Enable/disable the mini-app (default: true)
WEB_APP_HOST=127.0.0.1                # Server host (default: 127.0.0.1)
WEB_APP_PORT=5000                      # Server port (default: 5000)
WEB_APP_URL=http://127.0.0.1:5000    # Public URL for Telegram (required for ngrok/production)

# Bot Configuration (existing)
BOT_TOKEN=your_bot_token_here
API_ID=your_api_id
API_HASH=your_api_hash
```

### 3. For Public/Remote Access (Important!)

If you want to access the mini-app from outside your local network:

#### Option A: Using Ngrok (Development)

```bash
# Install ngrok
# From: https://ngrok.com/download

# In another terminal, expose your Flask app
ngrok http 5000

# Update .env with the ngrok URL
WEB_APP_URL=https://xxxxx-xxxx-xxx.ngrok.io
```

#### Option B: Using a VPS (Production)

```bash
# Update .env with your domain
WEB_APP_URL=https://yourdomain.com
WEB_APP_HOST=0.0.0.0
WEB_APP_PORT=5000

# Use nginx/Apache as reverse proxy to handle HTTPS
```

**Important**: Telegram mini-apps MUST use HTTPS URLs in production.

## Running the Bot

The Flask app and Telegram bot will start automatically:

```bash
python main.py
```

You should see output like:

```
Flask Web App started at 127.0.0.1:5000 (Mini-App URL: http://127.0.0.1:5000)
✅ Web dashboard started at http://127.0.0.1:8080
```

## Using the Mini-App

### From Telegram

1. **Start the bot**: `/start`
2. **Open file browser**: Click the "Open Modern File Browser" button, or use `/browse`
3. **Inside the mini-app**:
   - Click folders to navigate
   - Click files to select them
   - Use batch buttons for operations
   - Search for specific files

### Bot Commands

- `/browse` - Open the file browser mini-app
- `/files` - Traditional file listing (text mode)
- `/start` - Main menu with mini-app button

## API Endpoints

The Flask app provides these API endpoints (all require Telegram auth):

### File Management
- `GET /api/files` - List files in a directory
- `POST /api/files/delete` - Delete files
- `GET /api/files/search` - Search for files
- `GET /api/files/download/<filename>` - Download a file
- `GET /api/thumbnail/<filename>` - Get image thumbnail
- `POST /api/files/create-archive` - Create ZIP/7Z archive

### Statistics
- `GET /api/stats` - Get storage statistics

## Security Considerations

### Telegram Web App Validation
The app validates Telegram Web App `initData` signatures to ensure requests come from Telegram. This is optional by default but can be enabled:

```python
# In app/web/app.py, line ~86
if not validate_telegram_init_data(init_data):
    return jsonify({'error': 'Unauthorized'}), 401
```

### HTTPS Requirement
Always use HTTPS in production. Telegram mini-apps will refuse to load over HTTP.

### Directory Access Control
The app prevents directory traversal attacks by validating all file paths:

```python
# Security check (example from app/web/app.py)
if not str(file_path).startswith(str(app.download_dir)):
    return jsonify({'error': 'Access denied'}), 403
```

## Troubleshooting

### Mini-app button not appearing
- Check `WEB_APP_ENABLE=true` in `.env`
- Verify `WEB_APP_URL` is set correctly
- Make sure Flask app is running (check logs)

### "Cannot load mini-app" error
- Verify `WEB_APP_URL` uses HTTPS (if accessing from outside localhost)
- Check that the URL is publicly accessible
- Ensure the Flask app is running on that URL

### Files not appearing
- Check `DOWNLOAD_DIR` is set correctly
- Verify bot process has read permission on the directory
- Check Flask app logs for errors

### Upload/Delete not working
- Check bot has write permission on `DOWNLOAD_DIR`
- Verify Pyrogram client is running (for uploads)
- Check `app/web/app.py` logs for errors

## Customization

### Styling
Edit `app/web/static/style.css` to customize colors and layout:

```css
:root {
    --primary-color: #0088cc;      /* Blue */
    --danger-color: #ff3b30;       /* Red */
    --success-color: #34c759;      /* Green */
    /* ... more colors ... */
}
```

### Features
To add new features:

1. Add API endpoint in `app/web/app.py`
2. Add UI button/modal in `app/web/templates/index.html`
3. Add JavaScript handler in `app/web/static/app.js`

## File Structure

```
app/web/
├── __init__.py
├── app.py                  # Flask app and API endpoints
├── templates/
│   └── index.html         # Mini-app HTML
└── static/
    ├── style.css          # Styling
    └── app.js             # Frontend logic
```

## Performance Notes

- Thumbnail generation is on-demand (first request may be slow)
- Search is limited to 100 results
- Archive creation streams directly (no temp files)
- UI is optimized for mobile devices

## Known Limitations

- Archive passwords are not yet supported (ZIP only)
- Maximum upload size is 100MB
- Batch operations have no progress bar UI (backend shows progress)
- Video thumbnails require ffmpeg

## Future Enhancements

- [ ] Progress tracking for batch operations
- [ ] Drag-and-drop file organization
- [ ] File preview modal
- [ ] Scheduled cleanup tasks
- [ ] Download speed limiting
- [ ] Advanced filtering (by date, size, type)

## Support

For issues or feature requests:
1. Check the troubleshooting section
2. Review Flask app logs: `app/web/` directory
3. Check bot logs for errors
4. Verify environment configuration

---

**Version**: 1.0.0  
**Last Updated**: 2024-2026
