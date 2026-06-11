# -*- coding: utf-8 -*-

import asyncio
import hashlib
import logging
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import unquote_plus

# Load environment variables from .env file before any config is read.
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

from app.bot.dashboard import start_dashboard_server, stop_dashboard_server
from app.web.app import create_web_app

load_dotenv()

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

import yt_dlp
from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from app.downloaders.base import DownloadRequest
from app.downloaders.spotify import SpotifyDownloader, is_spotify_url

# Import torrent crawler subsystems
from app.downloaders.torrents.tpb import TPBCrawler, TPBHandlers
from app.downloaders.torrents.tpb.keyboards import tpb_categories_keyboard

# Import post downloader module for handling forwarded posts
from app.handlers.forwarded_media import setup_pyrogram_forwarded_downloads
from app.infrastructure.aria2_rpc import Aria2DaemonConfig, Aria2RpcClient, Aria2RpcError

# Import zipping utilities module
from app.services.archive import (
    MAX_ZIP_PART_SIZE,
    ZIP_LOCKS,
    ZipProgress,
    check_archive_format_support,
    check_password_support,
    filter_files_for_archiving,
    get_oversized_file_warnings,
)
from app.services.archive import human_size as zip_human_size
from app.services.archive import (
    make_archive_with_progress,
    render_progress_bar,
)
from app.services.archive import sanitize_filename as zip_sanitize
from app.services.hentai_playlist import (
    HentaiPlaylist,
    is_hentai_playlist_url,
    resolve_hentai_playlist,
)
from app.services.manga import (
    convert_images_to_pdf,
    download_manga_gallery,
    extract_manga_url,
    is_manga_url,
    list_manga_images,
    remove_manga_folder_if_empty,
)

# Import thumbnail generation module
from app.services.thumbnails import generate_contact_sheet

# Import zip settings module
from app.services.user_settings import (
    format_settings_text,
    get_setting,
    get_user_settings,
    save_user_settings,
    update_setting,
    validate_compression_level,
    validate_part_size,
    validate_password,
)
from app.services.video_sites import (
    is_adult_video_url,
    is_hentai_video_url,
    is_supported_video_url,
    video_platform_label,
    video_platform_slug,
)

MAX_ZIP_FILES = float('inf')  # No limit on number of files to zip
BOT_MAX_DOCUMENT_BYTES = 49 * 1024 * 1024  # Telegram Bot API document limit


# =========================================================
# Config
# =========================================================

BASE_DIR = Path(__file__).resolve().parents[2]
DOWNLOAD_DIR = BASE_DIR / "Download"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
TELEGRAM_DIR = BASE_DIR / "Download" / "Telegram"
TELEGRAM_DIR.mkdir(parents=True, exist_ok=True)
SPOTIFY_DIR = BASE_DIR / "Download" / "Spotify"
SPOTIFY_DIR.mkdir(parents=True, exist_ok=True)
MANGA_DIR = BASE_DIR / "Download" / "Manga"
MANGA_DIR.mkdir(parents=True, exist_ok=True)
ADULT_VIDEO_DIR = BASE_DIR / "Download" / "Adult"
ADULT_VIDEO_DIR.mkdir(parents=True, exist_ok=True)
HENTAI_VIDEO_DIR = BASE_DIR / "Download" / "Hentai"
HENTAI_VIDEO_DIR.mkdir(parents=True, exist_ok=True)


def parse_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw or default)
    except (TypeError, ValueError):
        logging.getLogger(__name__).warning(
            "Invalid integer for %s=%r; using %s",
            name,
            raw,
            default,
        )
        return default

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = parse_env_int("API_ID", 0)
API_HASH = os.getenv("API_HASH", "").strip()

PYRO_SESSION_NAME = os.getenv("PYRO_SESSION_NAME", "pyrogram_uploader")
ARIA2_BIN = os.getenv("ARIA2_BIN", "aria2c")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
SPOTDL_BIN = os.getenv("SPOTDL_BIN", "spotdl")
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "").strip()
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip()
ARIA2_RPC_HOST = os.getenv("ARIA2_RPC_HOST", "127.0.0.1").strip() or "127.0.0.1"
ARIA2_RPC_PORT = parse_env_int("ARIA2_RPC_PORT", 6800)
ARIA2_RPC_SECRET = os.getenv("ARIA2_RPC_SECRET", "").strip()

# TPB crawler config
TPB_API_URL = os.getenv("TPB_API_URL", "").strip()

FILES_PER_PAGE = 8
MAX_SEND_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB

pyro_client = None
upload_jobs = {}  # {job_id: {"status": "pending|uploading|completed|failed", "files": [...], ...}}
upload_queue = []  # Queue of upload job IDs
upload_counter = 0
upload_lock = asyncio.Lock()
mini_app_zip_jobs = {}

download_jobs = {}
job_counter = 0
jobs_lock = asyncio.Lock()
aria2_client = Aria2RpcClient(
    Aria2DaemonConfig(
        aria2_bin=ARIA2_BIN,
        download_dir=DOWNLOAD_DIR,
        rpc_host=ARIA2_RPC_HOST,
        rpc_port=ARIA2_RPC_PORT,
        rpc_secret=ARIA2_RPC_SECRET,
    )
)

# Short in-memory tokens for callback_data (with timestamp-based expiration)
path_tokens = {}  # {token: (rel_path, timestamp)}
reverse_path_tokens = {}  # {rel_path: token}
path_token_counter = 0
PATH_TOKEN_TIMEOUT = 3600  # Token expires after 1 hour (in seconds)

# Torrent file selection sessions
torrent_select_sessions = {}

# Batch file selection sessions for multi-select upload
batch_select_sessions = {}  # {user_id: {"rel_path", "selected", "page", "mode": "upload"|"delete"}}

# Zip file selection sessions
zip_select_sessions = {}  # {user_id: {"selected": set(), "page": int}}

# Pending zip name sessions (waiting for user to provide zip file name)
pending_zip_name_sessions = {}  # {user_id: {"mode": "all"|"selected", "session": {...}}}

# User language preferences
user_languages = {}  # {user_id: "en" or "fa"}
DEFAULT_LANGUAGE = "en"

# Status refresh tracking - one dashboard per chat
status_messages = {}  # {chat_id: {"message_id": int, "last_update": float}}
status_message_locks = {}  # {chat_id: asyncio.Lock}
STATUS_AUTO_UPDATE_SECONDS = 5
dashboard_messages = {}  # {chat_id: {"message_id": int, "last_update": float}}

# Pinned live dashboard tracking
live_dashboard_tasks = {}  # {chat_id: asyncio.Task}
pinned_dashboard_messages = {}  # {chat_id: message_id}

# Pending yt-dlp conversion selections
pending_ytdlp_requests = {}  # {user_id: {"url": str, "chat_id": int}}

# Pending Spotify confirmations
pending_spotify_requests = {}  # {user_id: {"url": str, "chat_id": int}}

# Pending manga gallery confirmations
pending_manga_requests = {}  # {user_id: {"url": str, "chat_id": int}}

# Pending hentai playlist confirmations
pending_hentai_playlist_requests = {}  # {user_id: {"playlist": HentaiPlaylist, "chat_id": int}}

# Dedicated executors for CPU-intensive operations
# Using ProcessPoolExecutor for py7zr since it's CPU-bound
# Limiting to 2 workers to prevent bot overload
zip_executor = None  # Will be initialized in post_init


# Logging
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("telegram_downloader_bot")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = RotatingFileHandler(
        LOG_DIR / "bot.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8"
    )
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# Security whitelist
ALLOWED_USER_IDS = {
    int(x.strip())
    for x in (os.getenv("ALLOWED_USER_IDS", "") or "").split(",")
    if x.strip().isdigit()
}

# Auto cleanup
AUTO_CLEANUP_DAYS = parse_env_int("AUTO_CLEANUP_DAYS", 7)

# Web dashboard configuration
WEB_DASHBOARD_ENABLE = os.getenv("WEB_DASHBOARD_ENABLE", "false").strip().lower() in {"1", "true", "yes", "on"}
WEB_DASHBOARD_HOST = os.getenv("WEB_DASHBOARD_HOST", "127.0.0.1").strip()
WEB_DASHBOARD_PORT = parse_env_int("WEB_DASHBOARD_PORT", 8080)

# Web App Mini-App configuration
WEB_APP_ENABLE = os.getenv("WEB_APP_ENABLE", "true").strip().lower() in {"1", "true", "yes", "on"}
WEB_APP_HOST = os.getenv("WEB_APP_HOST", "127.0.0.1").strip()
WEB_APP_PORT = parse_env_int("WEB_APP_PORT", 5000)
WEB_APP_URL = os.getenv("WEB_APP_URL", f"http://{WEB_APP_HOST}:{WEB_APP_PORT}").strip()
MINI_APP_DEFAULT_CHAT_ID = parse_env_int("MINI_APP_DEFAULT_CHAT_ID", 0)

# =========================================================
# Language Support
# =========================================================

LANGUAGES = {
    "en": {
        "home": "🏠 Main Menu",
        "home_desc": "Use the keyboard below.",
        "folder": "📁 Download folder:",
        "target": "📤 Upload target:",
        "target_val": "Your own Telegram account (Saved Messages / me)",
        "help": "❓ Help",
        "magnet_help": "🧲 Send a magnet or direct file URL to start downloading.",
        "status_help": "📊 Status: show active downloads.",
        "queue_help": "📋 Queue: show all jobs.",
        "cancel_help": "🛑 Cancel: show active jobs.",
        "cancel_help2": "- Send: cancel <job_id> to cancel one.",
        "clear_help": "🧹 Clear: remove finished jobs from memory.",
        "files_help": "📁 Files: browse the Download folder.",
        "upload_help": "📤 File upload: choose a file and upload it.",
        "upload_folder_help": "📤 Folder upload: choose a folder and upload all files.",
        "notes": "ℹ️ Notes:",
        "upload_account": "- Upload target is your own Telegram account.",
        "pyrogram_user": "- Pyrogram must be logged in as a user account, not a bot.",
        "pyrogram_first_run": "- On first run, Pyrogram may ask for phone/code/2FA in console.",
        "status": "📊 Status",
        "no_active": "No active downloads.",
        "active_jobs": "Active jobs:",
        "queue": "📋 Queue",
        "no_jobs": "No jobs yet.",
        "upload_complete": "✅ Upload Complete",
        "folder_upload_complete": "✅ Folder Upload Complete",
        "downloading": "📥 Downloading",
        "name": "Name:",
        "state": "State:",
        "progress": "Progress:",
        "speed": "Speed:",
        "eta": "ETA:",
        "confirm_delete": "⚠️ Confirm Delete",
        "delete_type_folder": "Type: Folder",
        "delete_type_file": "Type: File",
        "delete_warning_folder": "⚠️ Warning: this deletes everything inside.",
        "delete_warning_file": "⚠️ Warning: this file will be permanently deleted.",
        "file_details": "📄 File Details",
        "folder_details": "📁 Folder Details",
        "path": "📍 Path:",
        "size": "📊 Size:",
        "modified": "📅 Modified:",
        "subfolders": "Subfolders:",
        "files": "Files:",
        "total_size": "📊 Total size:",
        "uploaded": "Uploaded:",
        "yes_upload": "✅ Yes, Upload",
        "yes_delete": "🗑 Yes, Delete",
        "yes_upload_all": "✅ Yes, Upload All",
        "yes_delete_folder": "🗑 Yes, Delete Folder",
        "cancelled": "Cancelled.",
        "unknown_input": "Unknown input.\nUse the keyboard below, send a magnet, direct file URL, or supported media link.",
        "usage": "Usage: cancel <job_id>",
        "not_found": "not found.",
        "deleted_successfully": "✅ Deleted successfully",
        "batch_upload": "📤 Batch Upload",
        "batch_delete": "🗑 Batch Delete",
        "delete_all": "🗑 Delete All",
        "delete_all_confirm": "⚠️ Delete All in Folder",
        "delete_all_warning": "This permanently deletes every file and folder in this directory.",
        "yes_delete_all": "🗑 Yes, Delete All",
        "batch_delete_files": "Delete {} Files",
        "batch_delete_confirm": "⚠️ Confirm Batch Delete",
        "yes_batch_delete": "🗑 Yes, Delete Selected",
        "deleted_count": "Deleted: {} file(s)",
        "delete_all_done": "Deleted {} file(s) and {} folder(s).",
        "select_files": "Tap files to select/deselect them.",
        "upload_files": "Upload {} Files",
        "select_at_least": "Select at least one file",
        "preparing": "Preparing upload...",
        "duplicate_detected": "⚠️ Duplicate detected:",
        "job_started": "Job #",
        "job_id": "Job ID:",
        "job_status": "[{}]",
        "magnet_received": "🧲 Magnet received",
        "started": "Started job #",
        "pid": "PID:",
        "file_browser": "📁 File Browser",
        "items": "items",
        "page": "Page",
        "tap_file": "Tap a file or folder below.",
        "back": "⬅️ Back",
        "next": "Next ➡️",
        "prev": "⬅️ Prev",
        "root": "📁 Root",
        "refresh": "🔄 Refresh",
        "up": "⬆️ Up",
        "home_btn": "🏠 Home",
        "open_folder": "📁 Open Folder",
        "upload_file": "📤 Upload File",
        "upload_all": "📤 Upload All Files",
        "delete_btn": "🗑 Delete",
        "delete_folder": "🗑 Delete Folder",
        "cancel_btn": "❌ Cancel",
        "delete_label": "📋 Live Dashboard",
        "job_number": "Job #{}",
        "part": "Part",
        "uploading": "📤 Uploading",
        "target_account": "Target: your own Telegram account",
        "parts_sent": "Parts sent:",
        "language": "🌐 Language",
        "select_language": "Select your language:",
        "first": "⏮️ First",
        "last": "⏭️ Last",
        "live_dashboard": "📊 Live Dashboard",
        "toggle_language": "🌐 Toggle Language",
        "en": "English 🇺🇸",
        "fa": "فارسی 🇮🇷",
        "delete_cancelled": "Delete cancelled.",
        "confirm_cancel_job": "⚠️ Confirm Cancel Job",
        "confirm_clear": "⚠️ Confirm Clear",
        "clear_warning": "⚠️ Warning: this will remove ALL finished jobs.",
        "cleared": "Cleared",
        "clear": "Clear",
        "convert": "🎬 Convert Quality",
        "send_thumbnail": "📸 Send Thumbnail",
        "zip_menu": "📦 Zip Menu",
        "list_files": "📋 List Files",
        "select_files": "☑️ Select Files to Zip",
        "zip_all": "📦 Zip All",
        "settings": "⚙️ Settings",
        "zip_part_size": "Zip Part Size (MB)",
        "zip_method": "Zip Method",
        "zip_password": "Zip Password",
        "auto_delete_files": "Auto-delete after zip",
        "auto_delete_zips": "Auto-delete zips after send",
        "auto_delete_upload": "Auto-delete after upload",
        "compression_level": "Compression Level",
        "confirm_changes": "✅ Confirm Changes",
        "cancel": "❌ Cancel",
        "zip_settings": "⚙️ Zip Settings",
        "files_selected": "Files Selected",
        "select_save": "Select files below, then tap Save",
        "no_files": "No files to zip",
        "zipping": "Creating zip archive...",
        "zip_complete": "✅ Zip complete!",
        "zip_error": "❌ Zip error",
        "sending_zip": "Sending zip files...",
        "uploading_volume": "📤 Uploading Volume {}/{}: {}",
        "upload_progress": "{} {} ({}%) ⏱ {}",
        "file_count": "{} file(s)",
        "invalid_value": "Invalid value",
        "invalid_archive_method": "Invalid archive method",
        "part_size_error": "Part size must be 100 MB – 5 GB",
        "compression_error": "Compression level must be 1–9",
        "password_too_long": "Password too long (max 100 characters)",
        "enter_zip_name": "📦 Enter a name for the zip file:",
        "zip_name_cancelled": "Zip name entry cancelled.",
        "error_occurred": "An error occurred. Please try again.",
        # TPB crawler strings
        "tpb_search": "🏴‍☠️ TPB Search",
        "tpb_welcome": "The Pirate Bay Search",
        "tpb_send_query": "Send a search query to find torrents on The Pirate Bay.",
        "tpb_fetching": "Fetching torrent details...",
        "tpb_starting_download": "Starting download...",
        "tpb_download_started": "Download started!",
        "tpb_paste_to_download": "Paste this magnet link to start downloading.",
        "tpb_tap_download": "Tap 📥 to download instantly",
        "select_category": "🔍 Select a category for: {}",
        "searching_query": "🔍 Searching: {}",
        "no_results": "No results found.",
        "results_for": "Results for: {}",
        "link": "Link",
        "seeders": "Seeders",
        "leechers": "Leechers",
        "uploaded_on": "Uploaded",
        "error_fetching_link": "Error fetching torrent details.",
        "send_magnet": "Send Magnet",
        "no_magnet_stored": "No magnet link stored for this bookmark.",
    },
    "fa": {
        "home": "🏠 منوی اصلی",
        "home_desc": "از صفحه کلید زیر استفاده کنید.",
        "folder": "📁 پوشه‌ی دانلود:",
        "target": "📤 مقصد آپلود:",
        "target_val": "حساب تلگرام شخصی خود (Saved Messages / من)",
        "help": "❓ راهنما",
        "magnet_help": "🧲 برای شروع دانلود، لینک مگنت یا لینک مستقیم فایل ارسال کنید.",
        "status_help": "📊 وضعیت: نمایش دانلودهای فعال.",
        "queue_help": "📋 صف: نمایش تمام کارها.",
        "cancel_help": "🛑 انصراف: نمایش کارهای فعال.",
        "cancel_help2": "- ارسال: cancel <job_id> برای انصراف از یکی.",
        "clear_help": "🧹 پاک‌کردن: حذف کارهای تمام‌شده از حافظه.",
        "files_help": "📁 فایل‌ها: مرور پوشه‌ی دانلود.",
        "upload_help": "📤 آپلود فایل: یک فایل انتخاب و آپلود کنید.",
        "upload_folder_help": "📤 آپلود پوشه: یک پوشه انتخاب و تمام فایل‌ها را آپلود کنید.",
        "notes": "ℹ️ یادداشت‌ها:",
        "upload_account": "- مقصد آپلود حساب شخصی تلگرام شما است.",
        "pyrogram_user": "- Pyrogram باید با حساب کاربری وارد شود، نه ربات.",
        "pyrogram_first_run": "- در اولین اجرا، Pyrogram ممکن است از شما برای تلفن/کد/2FA در کنسول بپرسد.",
        "status": "📊 وضعیت",
        "no_active": "دانلود فعالی نیست.",
        "active_jobs": "کارهای فعال:",
        "queue": "📋 صف",
        "no_jobs": "هیچ کاری موجود نیست.",
        "upload_complete": "✅ آپلود تکمیل شد",
        "folder_upload_complete": "✅ آپلود پوشه تکمیل شد",
        "downloading": "📥 در حال دانلود",
        "name": "نام:",
        "state": "وضعیت:",
        "progress": "پیشرفت:",
        "speed": "سرعت:",
        "eta": "زمان تخمینی:",
        "confirm_delete": "⚠️ تأیید حذف",
        "delete_type_folder": "نوع: پوشه",
        "delete_type_file": "نوع: فایل",
        "delete_warning_folder": "⚠️ اخطار: این موارد را در داخل حذف می‌کند.",
        "delete_warning_file": "⚠️ اخطار: این فایل به‌طور دائمی حذف خواهد شد.",
        "file_details": "📄 جزئیات فایل",
        "folder_details": "📁 جزئیات پوشه",
        "path": "📍 مسیر:",
        "size": "📊 اندازه:",
        "modified": "📅 ویرایش‌شده:",
        "subfolders": "زیرپوشه‌ها:",
        "files": "فایل‌ها:",
        "total_size": "📊 اندازه کل:",
        "uploaded": "آپلود‌شده:",
        "yes_upload": "✅ بله، آپلود کنید",
        "yes_delete": "🗑 بله، حذف کنید",
        "yes_upload_all": "✅ بله، تمام را آپلود کنید",
        "yes_delete_folder": "🗑 بله، پوشه را حذف کنید",
        "cancelled": "لغو شد.",
        "unknown_input": "ورودی نامعلوم.\nاز صفحه کلید زیر استفاده کنید یا لینک مگنت، لینک مستقیم فایل، یا لینک رسانه پشتیبانی‌شده ارسال کنید.",
        "usage": "نحوه استفاده: cancel <job_id>",
        "not_found": "پیدا نشد.",
        "deleted_successfully": "✅ با موفقیت حذف شد",
        "batch_upload": "📤 آپلود دسته‌ای",
        "batch_delete": "🗑 حذف دسته‌ای",
        "delete_all": "🗑 حذف همه",
        "delete_all_confirm": "⚠️ حذف همه در پوشه",
        "delete_all_warning": "این کار همه فایل‌ها و پوشه‌های این مسیر را برای همیشه حذف می‌کند.",
        "yes_delete_all": "🗑 بله، حذف همه",
        "batch_delete_files": "حذف {} فایل",
        "batch_delete_confirm": "⚠️ تأیید حذف دسته‌ای",
        "yes_batch_delete": "🗑 بله، حذف انتخاب‌شده‌ها",
        "deleted_count": "حذف شد: {} فایل",
        "delete_all_done": "{} فایل و {} پوشه حذف شد.",
        "select_files": "فایل‌ها را بزنید تا انتخاب/لغو انتخاب شود.",
        "upload_files": "آپلود {} فایل",
        "select_at_least": "حداقل یک فایل انتخاب کنید",
        "preparing": "آماده‌سازی برای آپلود...",
        "duplicate_detected": "⚠️ تکراری شناسایی شد:",
        "job_started": "کار #",
        "job_id": "شناسه کار:",
        "job_status": "[{}]",
        "magnet_received": "🧲 لینک مغناطیسی دریافت شد",
        "started": "کار شروع شد #",
        "pid": "PID:",
        "file_browser": "📁 مرورگر فایل",
        "items": "مورد",
        "page": "صفحه",
        "tap_file": "روی فایل یا پوشه‌ای در زیر بزنید.",
        "back": "⬅️ بازگشت",
        "next": "بعدی ➡️",
        "prev": "⬅️ قبلی",
        "root": "📁 ریشه",
        "refresh": "🔄 بازخوانی",
        "up": "⬆️ بالا",
        "home_btn": "🏠 خانه",
        "open_folder": "📁 باز کردن پوشه",
        "upload_file": "📤 آپلود فایل",
        "upload_all": "📤 آپلود تمام فایل‌ها",
        "delete_btn": "🗑 حذف",
        "delete_folder": "🗑 حذف پوشه",
        "cancel_btn": "❌ انصراف",
        "delete_label": "📋 داشبورد زنده",
        "job_number": "کار #{}",
        "part": "قسمت",
        "uploading": "📤 در حال آپلود",
        "target_account": "مقصد: حساب شخصی تلگرام",
        "parts_sent": "قسمت‌های ارسال‌شده:",
        "language": "🌐 زبان",
        "select_language": "زبان خود را انتخاب کنید:",
        "first": "⏮️ اول",
        "last": "⏭️ آخر",
        "live_dashboard": "📊 داشبورد زنده",
        "toggle_language": "🌐 تعویض زبان",
        "en": "English 🇺🇸",
        "fa": "فارسی 🇮🇷",
        "delete_cancelled": "حذف لغو شد.",
        "confirm_cancel_job": "⚠️ تأیید لغو کار",
        "confirm_clear": "⚠️ تأیید پاک‌کردن",
        "clear_warning": "⚠️ اخطار: این تمام کارهای تمام‌شده را حذف می‌کند.",
        "cleared": "پاک شد",
        "clear": "پاک‌کردن",
        "convert": "🎬 تبدیل کیفیت",
        "send_thumbnail": "📸 ارسال ریزنمونه",
        "zip_menu": "📦 منوی فشرده‌سازی",
        "list_files": "📋 فهرست فایل‌ها",
        "select_files": "☑️ انتخاب فایل‌ها برای فشرده‌سازی",
        "zip_all": "📦 فشرده‌سازی تمام",
        "settings": "⚙️ تنظیمات",
        "zip_part_size": "اندازه قسمت فشرده‌سازی (مگابایت)",
        "zip_method": "روش فشرده‌سازی",
        "zip_password": "رمز فشرده‌سازی",
        "auto_delete_files": "حذف خودکار پس از فشرده‌سازی",
        "auto_delete_zips": "حذف خودکار فایل‌های فشرده بعد از ارسال",
        "auto_delete_upload": "حذف خودکار پس از آپلود",
        "compression_level": "سطح فشرده‌سازی",
        "confirm_changes": "✅ تأیید تغییرات",
        "cancel": "❌ لغو",
        "zip_settings": "⚙️ تنظیمات فشرده‌سازی",
        "files_selected": "فایل‌های انتخاب‌شده",
        "select_save": "فایل‌ها را در زیر انتخاب کنید، سپس ذخیره کنید",
        "no_files": "فایلی برای فشرده‌سازی وجود ندارد",
        "zipping": "ایجاد بایگانی فشرده‌سازی شده...",
        "zip_complete": "✅ فشرده‌سازی کامل شد!",
        "zip_error": "❌ خطای فشرده‌سازی",
        "sending_zip": "ارسال فایل‌های فشرده...",
        "uploading_volume": "📤 در حال ارسال جلد {}/{}: {}",
        "upload_progress": "{} {} ({}%) ⏱ {}",
        "file_count": "{} فایل",
        "invalid_value": "مقدار نامعتبر",
        "invalid_archive_method": "روش بایگانی نامعتبر",
        "part_size_error": "اندازه قسمت باید 100 مگابایت تا 5 گیگابایت باشد",
        "compression_error": "سطح فشرده‌سازی باید 1 تا 9 باشد",
        "password_too_long": "رمز بسیار طولانی است (حداکثر 100 کاراکتر)",
        "enter_zip_name": "📦 یک نام برای فایل فشرده وارد کنید:",
        "zip_name_cancelled": "ورود نام فشرده‌سازی لغو شد.",
        "error_occurred": "An error occurred. Please try again.",
        # TPB crawler strings
        "tpb_search": "🏴‍☠️ جستجوی TPB",
        "tpb_welcome": "جستجوی The Pirate Bay",
        "tpb_send_query": "یک عبارت جستجو برای یافتن تورنت در The Pirate Bay ارسال کنید.",
        "tpb_fetching": "در حال دریافت جزئیات تورنت...",
        "tpb_starting_download": "در حال شروع دانلود...",
        "tpb_download_started": "دانلود شروع شد!",
        "tpb_paste_to_download": "این لینک مغناطیسی را برای شروع دانلود ارسال کنید.",
        "tpb_tap_download": "برای دانلود فوری 📥 را بزنید",
        "select_category": "🔍 دسته‌بندی را انتخاب کنید: {}",
        "searching_query": "🔍 در حال جستجو: {}",
        "no_results": "نتیجه‌ای یافت نشد.",
        "results_for": "نتایج برای: {}",
        "link": "لینک",
        "seeders": "سیدر",
        "leechers": "لیچر",
        "uploaded_on": "آپلود شده",
        "error_fetching_link": "خطا در دریافت جزئیات تورنت.",
        "send_magnet": "ارسال لینک مغناطیسی",
        "no_magnet_stored": "لینک مغناطیسی برای این نشانک ذخیره نشده.",
    }
}


def get_lang(user_id: int, key: str) -> str:
    """Get translated string for user."""
    lang = user_languages.get(user_id, DEFAULT_LANGUAGE)
    return LANGUAGES.get(lang, LANGUAGES[DEFAULT_LANGUAGE]).get(key, key)


def get_lang_for_all(key: str, lang: str = DEFAULT_LANGUAGE) -> str:
    """Get translated string for a specific language."""
    return LANGUAGES.get(lang, LANGUAGES[DEFAULT_LANGUAGE]).get(key, key)


def is_authorized_user(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def find_duplicate_by_hash(target_file: Path):
    try:
        target_hash = sha256_file(target_file)
    except Exception:
        return None

    for root, _, files in os.walk(DOWNLOAD_DIR):
        for name in files:
            candidate = Path(root) / name

            try:
                if candidate.resolve() == target_file.resolve():
                    continue

                if candidate.stat().st_size != target_file.stat().st_size:
                    continue

                if sha256_file(candidate) == target_hash:
                    return str(candidate.relative_to(DOWNLOAD_DIR))
            except Exception:
                continue

    return None


async def auto_cleanup_old_files():
    now = time.time()
    max_age = AUTO_CLEANUP_DAYS * 86400

    for root, _, files in os.walk(DOWNLOAD_DIR):
        for file_name in files:
            path = Path(root) / file_name

            try:
                age = now - path.stat().st_mtime

                if age > max_age:
                    if "_part_" in file_name or ".thumb_" in file_name:
                        path.unlink(missing_ok=True)

            except Exception:
                continue


def reset_browser_root(context):
    context.user_data["current_dir"] = str(DOWNLOAD_DIR)

# =========================================================
# Emoji-safe UI constants
# =========================================================

ICON_HOME = "\U0001F3E0"        # 🏠
ICON_FOLDER = "\U0001F4C1"      # 📁
ICON_FILE = "\U0001F4C4"        # 📄
ICON_VIDEO = "\U0001F39E"       # 🎞
ICON_AUDIO = "\U0001F3B5"       # 🎵
ICON_IMAGE = "\U0001F5BC"       # 🖼
ICON_ARCHIVE = "\U0001F5DC"     # 🗜
ICON_MAGNET = "\U0001F9F2"      # 🧲
ICON_STATUS = "\U0001F4CA"      # 📊
ICON_QUEUE = "\U0001F4CB"       # 📋
ICON_UPLOAD = "\U0001F4E4"      # 📤
ICON_DOWNLOAD = "\U0001F4E5"    # 📥
ICON_DELETE = "\U0001F5D1"      # 🗑
ICON_INFO = "\u2139"            # ℹ
ICON_PIN = "\U0001F4CD"         # 📍
ICON_BOX = "\U0001F4E6"         # 📦
ICON_SPEED = "\U0001F680"       # 🚀
ICON_CLOCK = "\U000023F1"       # ⏱
ICON_WARN = "\u26A0"            # ⚠
ICON_OK = "\u2705"              # ✅
ICON_FAIL = "\u274C"            # ❌
ICON_UP = "\u2B06"              # ⬆
ICON_BACK = "\u2B05"            # ⬅
ICON_NEXT = "\u27A1"            # ➡
ICON_REFRESH = "\U0001F504"     # 🔄
ICON_HELP = "\u2753"            # ❓
ICON_BROOM = "\U0001F9F9"       # 🧹
ICON_STOP = "\U0001F6D1"        # 🛑
ICON_SETTINGS = "\u2699"        # ⚙
ICON_LANGUAGE = "\U0001F310"    # 🌐


EMOJI_PREFIX_RE = re.compile(r"^[\W_]*[\U0001F300-\U0001FAFF\u2600-\u27BF\u2139\uFE0F]+\s*")

def clean_emoji_prefix(text: str) -> str:
    """Remove leading emoji/icon prefixes to avoid duplicated icons in UI."""
    if not text:
        return text
    return EMOJI_PREFIX_RE.sub("", text).strip()


# =========================================================
# Helpers
# =========================================================

def human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    s = float(size)
    for unit in units:
        if s < 1024 or unit == units[-1]:
            return f"{int(s)} B" if unit == "B" else f"{s:.2f} {unit}"
        s /= 1024
    return f"{size} B"


def human_speed(size: float) -> str:
    return f"{human_size(int(size))}/s"


def safe_join(base: Path, rel_path: str) -> Path:
    base_abs = base.resolve()
    target = (base_abs / rel_path).resolve()
    if not (target == base_abs or str(target).startswith(str(base_abs) + os.sep)):
        raise ValueError("Invalid path")
    return target


def collect_download_files() -> list:
    try:
        return sorted([f for f in DOWNLOAD_DIR.rglob("*") if f.is_file()])
    except Exception:
        return []


def apply_zip_file_limit(files: list, limit: int = MAX_ZIP_FILES) -> tuple:
    if len(files) <= limit:
        return files, None
    return files[:limit], f"⚠️ Only the first {limit} of {len(files)} files were included."


def archive_kwargs_from_settings(settings: dict) -> dict:
    password = (settings.get("password") or "").strip() or None
    try:
        compression_level = int(settings.get("compression_level", 5))
    except (TypeError, ValueError):
        compression_level = 5
    compression_level = max(1, min(9, compression_level))
    try:
        max_part_size = int(settings.get("zip_part_size", MAX_ZIP_PART_SIZE))
    except (TypeError, ValueError):
        max_part_size = MAX_ZIP_PART_SIZE
    method = (settings.get("zip_method") or "zip").lower()
    if method not in ("zip", "7z"):
        method = "zip"
    return {
        "password": password,
        "max_part_size": max(1, max_part_size),
        "archive_format": method,
        "compression_level": compression_level,
    }


def file_rel_path(path: Path) -> str:
    return str(path.relative_to(DOWNLOAD_DIR)).replace("\\", "/")


def build_files_to_zip(file_paths: list) -> list:
    return [
        (i + 1, file_rel_path(f), f, f.stat().st_size)
        for i, f in enumerate(file_paths)
    ]


def resolve_selected_zip_files(session: dict) -> list:
    """Resolve encoded path tokens from a zip selection session."""
    selected = []
    for token in session.get("selected", set()):
        try:
            rel = decode_path(token)
            full = safe_join(DOWNLOAD_DIR, rel)
            if full.is_file():
                selected.append(full)
        except Exception:
            continue
    return sorted(selected, key=lambda p: str(p).lower())


def format_archive_progress_text(progress: ZipProgress, prefix: str = "📦") -> str:
    snap = progress.snapshot()
    stage = snap["stage"]
    if stage == "splitting":
        part_info = ""
        if snap["total_parts"]:
            part_info = f"\nVolume {snap['current_part']}/{snap['total_parts']}"
        return f"{prefix} Splitting archive into volumes...{part_info}"
    total_b = snap["total_bytes"] or 1
    pct = snap["done_bytes"] / total_b * 100
    bar = render_progress_bar(pct)
    part_info = ""
    if snap["total_parts"] > 1:
        part_info = f"\nVolume {snap['current_part']}/{snap['total_parts']}"
    current = snap["current_file"]
    file_line = f"\n{current[:50]}" if current else ""
    return (
        f"{prefix} {stage.title()}...{part_info}\n"
        f"{bar} {pct:.0f}%\n"
        f"Files: {snap['done_files']}/{snap['total_files']}"
        f"{file_line}"
    )


async def maybe_delete_file_after_upload(user_id: int, rel_path: str) -> None:
    if not user_id:
        return
    settings = get_user_settings(user_id)
    if not settings.get("auto_delete_files_after_upload"):
        return
    try:
        full = safe_join(DOWNLOAD_DIR, rel_path)
        if full.is_file():
            full.unlink()
    except Exception as e:
        logger.warning(f"auto_delete after upload failed for {rel_path}: {e}")


def create_zip_upload_callback(
    context, chat_id: int, user_id: int, settings: dict, status_msg=None
):
    """Create a callback that uploads each zip part immediately after creation and deletes it from disk."""
    
    async def upload_and_delete_async(part_path: Path, current_vol: int, total_vols: int):
        """Async function to upload and delete a part"""
        try:
            size = part_path.stat().st_size
            via = "bot" if size <= BOT_MAX_DOCUMENT_BYTES else "pyrogram"
            caption = f"📦 Volume {current_vol}/{total_vols}: {part_path.name}\nSize: {zip_human_size(size)}"
            if total_vols > 1 and current_vol == 1:
                caption += "\nOpen the .001 file in WinRAR or 7-Zip to extract everything."
            if via == "pyrogram":
                caption += "\n(Large volume sent via Pyrogram)"
            
            # Update progress message before sending
            if status_msg and user_id:
                try:
                    pct = int((current_vol - 1) / total_vols * 100) if total_vols > 0 else 0
                    bar = render_progress_bar(pct)
                    progress_text = (
                        f"📤 {get_lang(user_id, 'uploading_volume').format(current_vol, total_vols, part_path.name)}\n"
                        f"{bar} {pct}% - Uploading instantly (auto-deleting from disk)...\n\n"
                        f"📊 Saving VPS disk space by deleting each part after upload"
                    )
                    await status_msg.edit_text(progress_text)
                except Exception as e:
                    logger.debug(f"Progress update failed: {e}")
            
            # Send the archive
            ok = await send_archive_document(context, chat_id, part_path, caption)
            if not ok:
                return False, f"Failed to send volume {current_vol}"
            
            # Delete the part from disk to save space
            try:
                part_path.unlink()
                logger.info(f"Deleted zip part from disk after upload: {part_path.name}")
            except Exception as e:
                logger.warning(f"Could not delete zip part {part_path.name}: {e}")
            
            # Return True to indicate we handled the deletion
            return True, None
        except Exception as e:
            logger.error(f"Error in upload callback for {part_path.name}: {e}")
            return False, str(e)
    
    def sync_upload_callback(part_path: Path, current_vol: int, total_vols: int) -> tuple:
        """Sync wrapper for the async callback - called from executor thread"""
        try:
            # Get the main event loop
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                # No event loop in this thread
                logger.warning("Could not get event loop for upload callback")
                return False, "No event loop available"
            
            # Schedule the async function on the main event loop
            future = asyncio.run_coroutine_threadsafe(
                upload_and_delete_async(part_path, current_vol, total_vols),
                loop
            )
            
            # Wait for the result with a reasonable timeout (5 minutes per part)
            deleted, error = future.result(timeout=300)
            return deleted, error
        except Exception as e:
            logger.error(f"Sync upload callback error: {e}")
            return False, str(e)
    
    return sync_upload_callback


async def run_archive_job(
    user_id: int,
    files_to_zip: list,
    output_dir: Path,
    zip_name: str,
    settings: dict,
    on_progress=None,
    upload_callback=None,
) -> tuple:
    if not files_to_zip:
        raise RuntimeError("No files to archive")

    kwargs = archive_kwargs_from_settings(settings)
    password = kwargs.pop("password")
    warnings = get_oversized_file_warnings(files_to_zip, kwargs["max_part_size"])

    fmt_err = check_archive_format_support(kwargs["archive_format"])
    if fmt_err:
        raise RuntimeError(fmt_err)
    pwd_err = check_password_support(password, kwargs["archive_format"])
    if pwd_err:
        raise RuntimeError(pwd_err)

    progress = ZipProgress()
    loop = asyncio.get_running_loop()

    def worker():
        return make_archive_with_progress(
            files_to_zip,
            output_dir,
            zip_name=zip_name,
            password=password,
            progress=progress,
            on_volume_created=upload_callback,
            **kwargs,
        )

    async with ZIP_LOCKS[user_id]:
        # Use dedicated zip_executor for CPU-intensive compression
        # This prevents the default thread pool from being exhausted
        executor = zip_executor if zip_executor else None
        task = loop.run_in_executor(executor, worker)
        
        # Update progress every 1 second instead of 2 for more responsive feedback
        while not task.done():
            if on_progress:
                try:
                    await on_progress(format_archive_progress_text(progress))
                except BadRequest as e:
                    if "message is not modified" not in str(e).lower():
                        logger.debug(f"Progress update skipped: {e}")
                except Exception as e:
                    logger.debug(f"Progress update failed: {e}")
            await asyncio.sleep(1.0)  # More frequent updates
        
        if on_progress:
            try:
                await on_progress(format_archive_progress_text(progress))
            except Exception:
                pass
        
        paths = await task
        if not paths:
            raise RuntimeError("No archives were created")
        return paths, warnings


async def send_archive_document(context, chat_id: int, archive_path: Path, caption: str) -> bool:
    """Send an archive via Bot API or Pyrogram depending on size."""
    size = archive_path.stat().st_size
    try:
        if size <= BOT_MAX_DOCUMENT_BYTES:
            with open(archive_path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=caption,
                )
        else:
            client = await get_pyrogram_client()
            await send_with_flood_wait_handling(
                lambda: client.send_document(
                    chat_id=chat_id,
                    document=str(archive_path),
                    caption=caption,
                )
            )
        return True
    except Exception as e:
        logger.error(f"Error sending archive {archive_path.name}: {e}")
        return False


async def send_archives_to_chat(context, chat_id: int, zip_paths: list, settings: dict, status_msg=None, user_id: int = None) -> bool:
    """Send all archives with upload progress; delete each only after successful send. Returns True if all succeeded."""
    all_ok = True
    total = len(zip_paths)
    
    # Calculate total size for progress tracking
    total_size = sum(p.stat().st_size for p in zip_paths)
    sent_size = 0
    
    for i, archive_path in enumerate(zip_paths, 1):
        size = archive_path.stat().st_size
        via = "bot" if size <= BOT_MAX_DOCUMENT_BYTES else "pyrogram"
        caption = f"📦 Volume {i}/{total}: {archive_path.name}\nSize: {zip_human_size(size)}"
        if total > 1 and i == 1:
            caption += "\nOpen the .001 file in WinRAR or 7-Zip to extract everything."
        if via == "pyrogram":
            caption += "\n(Large volume sent via Pyrogram)"
        
        # Update progress message before sending
        if status_msg and user_id:
            try:
                pct = int((sent_size / total_size * 100)) if total_size > 0 else 0
                bar = render_progress_bar(pct)
                progress_text = (
                    f"📤 {get_lang(user_id, 'uploading_volume').format(i, total, archive_path.name)}\n"
                    f"{bar} {pct}% ({zip_human_size(sent_size)}/{zip_human_size(total_size)})\n\n"
                    f"Status: Uploading..."
                )
                await status_msg.edit_text(progress_text)
            except Exception as e:
                logger.debug(f"Progress update failed: {e}")
        
        # Send the archive
        ok = await send_archive_document(context, chat_id, archive_path, caption)
        if not ok:
            all_ok = False
            continue
        
        sent_size += size
        
        # Delete after successful send
        if settings.get("auto_delete_zips_after_send"):
            try:
                archive_path.unlink()
            except Exception as e:
                logger.warning(f"Could not delete archive {archive_path}: {e}")
    
    return all_ok


def encode_path(rel_path: str) -> str:
    """FIX #4: Encode path with timestamp-based token expiration to prevent memory leaks."""
    global path_token_counter

    if not rel_path:
        return ""

    rel_path = str(rel_path)

    # Check if we already have a valid token for this path
    if rel_path in reverse_path_tokens:
        token = reverse_path_tokens[rel_path]
        if token in path_tokens:
            stored_path, timestamp = path_tokens[token]
            if time.time() - timestamp < PATH_TOKEN_TIMEOUT:
                # Token is still valid, reuse it
                return token

    # Clean up expired tokens to prevent memory leaks
    current_time = time.time()
    expired_tokens = [token for token, (path, ts) in path_tokens.items() 
                      if current_time - ts >= PATH_TOKEN_TIMEOUT]
    for token in expired_tokens:
        stored_path, _ = path_tokens.pop(token, (None, None))
        if stored_path in reverse_path_tokens:
            del reverse_path_tokens[stored_path]

    # Create new token
    path_token_counter_val = globals()['path_token_counter'] = globals()['path_token_counter'] + 1
    token = f"p{path_token_counter_val}"

    path_tokens[token] = (rel_path, current_time)
    reverse_path_tokens[rel_path] = token

    return token


def decode_path(token: str) -> str:
    if not token:
        return ""

    if token not in path_tokens:
        raise ValueError("Path token expired. Please open Files again.")

    rel_path, timestamp = path_tokens[token]
    
    # Check if token has expired
    if time.time() - timestamp >= PATH_TOKEN_TIMEOUT:
        # Clean up expired token
        del path_tokens[token]
        if rel_path in reverse_path_tokens:
            del reverse_path_tokens[rel_path]
        raise ValueError("Path token expired. Please open Files again.")

    return rel_path


def rel_parent(rel_path: str) -> str:
    if not rel_path:
        return ""
    parent = os.path.dirname(rel_path.rstrip("/"))
    return "" if parent == "." else parent


def rel_name(rel_path: str) -> str:
    if not rel_path:
        return "/"
    return os.path.basename(rel_path.rstrip("/")) or "/"


def extract_bt_name(magnet: str) -> str:
    m = re.search(r"[?&]dn=([^&]+)", magnet)
    if not m:
        return "Unknown torrent"
    try:
        return clean_download_name(unquote_plus(m.group(1)))
    except Exception:
        return "Unknown torrent"


def clean_download_name(name: str) -> str:
    name = unquote_plus(str(name or "")).strip()
    name = re.sub(r"^\[metadata\]\s*", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name.replace("+", " ")).strip()
    return name or "Unknown torrent"


DIRECT_HTTP_EXTENSIONS = {
    ".7z",
    ".apk",
    ".avi",
    ".bin",
    ".bz2",
    ".csv",
    ".deb",
    ".doc",
    ".docx",
    ".exe",
    ".flac",
    ".gz",
    ".iso",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".msi",
    ".pdf",
    ".png",
    ".rar",
    ".tar",
    ".tgz",
    ".torrent",
    ".txt",
    ".wav",
    ".webm",
    ".webp",
    ".xz",
    ".zip",
}


def extract_http_url(text: str) -> str:
    match = re.search(r"https?://\S+", text.strip(), re.IGNORECASE)
    if not match:
        return text.strip()
    return match.group(0).rstrip(").,]}")


def is_http_url(text: str) -> bool:
    return extract_http_url(text).lower().startswith(("http://", "https://"))


def extract_http_filename(url: str) -> str:
    url = extract_http_url(url)
    clean_url = url.split("?", 1)[0].split("#", 1)[0]
    name = Path(unquote_plus(clean_url)).name
    return clean_download_name(name) if name else "HTTP download"


def is_direct_http_download_url(text: str) -> bool:
    if not is_http_url(text):
        return False
    path = extract_http_url(text).split("?", 1)[0].split("#", 1)[0]
    return Path(path).suffix.lower() in DIRECT_HTTP_EXTENSIONS


def now_ts():
    return int(time.time())


def format_eta(seconds):
    if seconds is None or seconds < 0:
        return "Unknown"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


async def update_status_message(app: Application, chat_id: int, user_id: int = None):
    """Update existing status message or create a new one."""
    u = user_id or 0
    lock = status_message_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        await _update_status_message_unlocked(app, chat_id, u)


async def _update_status_message_unlocked(app: Application, chat_id: int, user_id: int):
    """Update the chat status dashboard while the per-chat lock is held."""
    u = user_id or 0
    try:
        if chat_id in status_messages:
            msg_data = status_messages[chat_id]
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_data["message_id"],
                    text=build_status_text(u),
                    reply_markup=build_status_controls_markup(),
                    disable_web_page_preview=True,
                )
                msg_data["last_update"] = time.time()
                msg_data["user_id"] = u
                return
            except Exception:
                # Message not found or expired, remove it
                del status_messages[chat_id]
        
        # Send new status message
        msg = await app.bot.send_message(
            chat_id=chat_id,
            text=build_status_text(u),
            reply_markup=build_status_controls_markup(),
            disable_web_page_preview=True,
        )
        status_messages[chat_id] = {
            "message_id": msg.message_id,
            "last_update": time.time(),
            "user_id": u,
        }
    except Exception:
        pass


async def maybe_auto_update_status_message(app: Application, job: dict, force: bool = False):
    if not job.get("status_visible", True):
        return

    msg_data = status_messages.get(job["chat_id"])
    if not msg_data:
        return

    now = time.time()
    if not force and now - msg_data.get("last_update", 0) < STATUS_AUTO_UPDATE_SECONDS:
        return

    await update_status_message(app, job["chat_id"], job.get("user_id") or msg_data.get("user_id") or 0)


async def update_live_dashboard(app: Application, chat_id: int, user_id: int = None):
    """Update or create live dashboard with pinned message."""
    u = user_id or 0
    try:
        dashboard_text = build_live_dashboard_text(u)
        
        if chat_id in pinned_dashboard_messages:
            msg_id = pinned_dashboard_messages[chat_id]
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_id,
                    text=dashboard_text,
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(f"{ICON_REFRESH} {clean_emoji_prefix(get_lang(u, 'refresh'))}", 
                                               callback_data="refresh_dashboard"),
                        ]
                    ]),
                    disable_web_page_preview=True,
                )
                return
            except Exception:
                # Message not found, create new one
                del pinned_dashboard_messages[chat_id]
        
        # Send new dashboard message
        msg = await app.bot.send_message(
            chat_id=chat_id,
            text=dashboard_text,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{ICON_REFRESH} {clean_emoji_prefix(get_lang(u, 'refresh'))}", 
                                       callback_data="refresh_dashboard"),
                ]
            ]),
            disable_web_page_preview=True,
        )
        
        # Try to pin the message
        try:
            await app.bot.pin_chat_message(chat_id=chat_id, message_id=msg.message_id)
        except Exception:
            pass
        
        pinned_dashboard_messages[chat_id] = msg.message_id
        dashboard_messages[chat_id] = {
            "message_id": msg.message_id,
            "last_update": time.time(),
        }
    except Exception:
        pass


async def live_dashboard_refresh_loop(app: Application, chat_id: int, user_id: int = None):
    """Periodically update the live dashboard."""
    u = user_id or 0
    while chat_id in pinned_dashboard_messages:
        try:
            await asyncio.sleep(2)
            await update_live_dashboard(app, chat_id, u)
        except Exception:
            break


def shorten(text: str, max_len: int = 38) -> str:
    text = text.strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def item_icon(name: str, is_dir: bool) -> str:
    if is_dir:
        return ICON_FOLDER

    ext = Path(name).suffix.lower()

    if ext in {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}:
        return ICON_VIDEO
    if ext in {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"}:
        return ICON_AUDIO
    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return ICON_IMAGE
    if ext in {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}:
        return ICON_ARCHIVE
    if ext in {".torrent"}:
        return ICON_MAGNET
    return ICON_FILE


def get_all_files_in_folder(rel_path: str):
    full = safe_join(DOWNLOAD_DIR, rel_path)
    if not full.is_dir():
        raise NotADirectoryError(str(full))

    out = []
    for root, _, files in os.walk(full):
        for f in sorted(files):
            p = Path(root) / f
            try:
                if p.is_file():
                    rel = str(p.relative_to(DOWNLOAD_DIR))
                    out.append(rel)
            except Exception:
                pass
    return sorted(out)


async def safe_edit_message(message, text, reply_markup=None):
    try:
        await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await message.reply_text(
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception:
            pass
    except Exception:
        try:
            await message.reply_text(
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except Exception:
            pass


def build_reply_menu(user_id: int = None):
    u = user_id or 0
    rows = [
        [f"{ICON_STATUS} Status"],
        [f"{ICON_DOWNLOAD} Downloads", f"{ICON_FOLDER} Files"],
        [f"{ICON_ARCHIVE} Tools", f"{ICON_SETTINGS} Settings"],
        [f"{ICON_HELP} Help"],
    ]
    if WEB_APP_ENABLE and WEB_APP_URL:
        rows.insert(0, [KeyboardButton("📱 Mini-App", web_app=WebAppInfo(url=WEB_APP_URL))])

    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=get_lang(u, 'magnet_help'),
    )

def build_downloads_menu_text(user_id: int = None) -> str:
    return (
        f"{ICON_DOWNLOAD} Downloads\n\n"
        "Send a magnet, torrent file, direct URL, Spotify link, manga/gallery link, "
        "or supported video link to start a download.\n\n"
        "Use the buttons below for status, search, and cleanup."
    )


def build_downloads_menu_markup(user_id: int = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ICON_STATUS} Live Status", callback_data="menu:status")],
        [
            InlineKeyboardButton(f"{ICON_MAGNET} TPB Search", callback_data="menu:tpb"),
        ],
        [InlineKeyboardButton(f"{ICON_BROOM} Clear Finished Jobs", callback_data="menu:clear")],
        [InlineKeyboardButton(f"{ICON_HOME} Main Menu", callback_data="menu_home")],
    ])


def build_files_menu_text(user_id: int = None) -> str:
    return (
        f"{ICON_FOLDER} Files\n\n"
        "Browse downloads, upload selected files to Telegram, delete files, or open "
        "the Mini-App for the cleanest file manager experience."
    )


def build_files_menu_markup(user_id: int = None) -> InlineKeyboardMarkup:
    rows = []
    if WEB_APP_ENABLE and WEB_APP_URL:
        rows.append([InlineKeyboardButton("📱 Open Mini-App", web_app=WebAppInfo(url=WEB_APP_URL))])
    rows.extend([
        [InlineKeyboardButton(f"{ICON_FOLDER} Chat File Browser", callback_data="menu:file_browser")],
        [InlineKeyboardButton(f"{ICON_ARCHIVE} Archive / Zip Menu", callback_data="menu:zip")],
        [InlineKeyboardButton(f"{ICON_HOME} Main Menu", callback_data="menu_home")],
    ])
    return InlineKeyboardMarkup(rows)


def build_tools_menu_text(user_id: int = None) -> str:
    return (
        f"{ICON_ARCHIVE} Tools\n\n"
        "Archive files, search torrents, adjust manga PDF behavior, or manage finished jobs."
    )


def build_tools_menu_markup(user_id: int = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ICON_ARCHIVE} Zip Menu", callback_data="menu:zip")],
        [
            InlineKeyboardButton(f"{ICON_MAGNET} TPB Search", callback_data="menu:tpb"),
        ],
        [InlineKeyboardButton(f"{ICON_IMAGE} Manga Settings", callback_data="menu:manga_settings")],
        [InlineKeyboardButton(f"{ICON_BROOM} Clear Finished Jobs", callback_data="menu:clear")],
        [InlineKeyboardButton(f"{ICON_HOME} Main Menu", callback_data="menu_home")],
    ])


def build_settings_menu_text(user_id: int = None) -> str:
    return (
        f"{ICON_SETTINGS} Settings\n\n"
        "Tune archive behavior, manga PDF automation, forwarded-post downloads, "
        "and language from one place."
    )


def build_settings_menu_markup(user_id: int = None) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id or 0)
    forwarded = "ON" if settings.get("auto_download_forwarded_posts") else "OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ICON_SETTINGS} Archive Settings", callback_data="menu:zip_settings")],
        [InlineKeyboardButton(f"{ICON_IMAGE} Manga Settings", callback_data="menu:manga_settings")],
        [InlineKeyboardButton(f"{ICON_DOWNLOAD} Forwarded Posts: {forwarded}", callback_data="menu:forwarded_posts")],
        [InlineKeyboardButton(f"{ICON_LANGUAGE} Language", callback_data="menu:language")],
        [InlineKeyboardButton(f"{ICON_HOME} Main Menu", callback_data="menu_home")],
    ])


# =========================================================
# Main texts
# =========================================================

def build_home_text(user_id: int = None):
    return (
        f"{get_lang(user_id or 0, 'home')}\n\n"
        f"{get_lang(user_id or 0, 'home_desc')}\n\n"
        f"{ICON_FOLDER} {clean_emoji_prefix(get_lang(user_id or 0, 'folder'))}\n{DOWNLOAD_DIR}\n\n"
        f"{ICON_UPLOAD} {clean_emoji_prefix(get_lang(user_id or 0, 'target'))}\n{get_lang(user_id or 0, 'target_val')}"
    )


def build_help_text(user_id: int = None):
    u = user_id or 0
    return (
        f"{ICON_HELP} {clean_emoji_prefix(get_lang(u, 'help'))}\n\n"
        f"{ICON_MAGNET} {clean_emoji_prefix(get_lang(u, 'magnet_help'))}\n"
        f"{ICON_STATUS} {clean_emoji_prefix(get_lang(u, 'status_help'))}\n"
        f"{ICON_STOP} Pause/resume/cancel active downloads from the status card.\n"
        f"{ICON_BROOM} {clean_emoji_prefix(get_lang(u, 'clear_help'))}\n"
        f"{ICON_FOLDER} {clean_emoji_prefix(get_lang(u, 'files_help'))}\n"
        f"{ICON_UPLOAD} {clean_emoji_prefix(get_lang(u, 'upload_help'))}\n"
        f"{ICON_UPLOAD} {clean_emoji_prefix(get_lang(u, 'upload_folder_help'))}\n\n"
        f"{ICON_DOWNLOAD} Forwarded posts: /forwardedposts on|off controls automatic forwarded media downloads.\n\n"
        f"{ICON_INFO} {clean_emoji_prefix(get_lang(u, 'notes'))}\n"
        f"- {get_lang(u, 'upload_account')}\n"
        f"- {get_lang(u, 'pyrogram_user')}\n"
        f"- {get_lang(u, 'pyrogram_first_run')}"
    )


def format_forwarded_posts_setting(user_id: int) -> str:
    settings = get_user_settings(user_id)
    enabled = settings.get("auto_download_forwarded_posts", False)
    return f"Forwarded post auto-download: {'ON' if enabled else 'OFF'}"


def build_status_text(user_id: int = None):
    u = user_id or 0
    active = [
        j
        for j in download_jobs.values()
        if j["status"] in JOB_ACTIVE_STATES and j.get("status_visible", True)
    ]

    if not active:
        return (
            f"{ICON_STATUS} Downloads\n\n"
            f"No active downloads.\n\n"
            f"{ICON_FOLDER} Folder\n{DOWNLOAD_DIR}\n\n"
            f"{ICON_UPLOAD} Upload target\n{get_lang(u, 'target_val')}"
        )

    lines = [
        f"{ICON_STATUS} Downloads",
        "",
        f"Active: {len(active)}",
        f"{ICON_UPLOAD} Upload target: {get_lang(u, 'target_val')}",
        ""
    ]

    for j in sorted(active, key=lambda x: x["id"]):
        lines.extend(format_download_status_lines(j))

    return "\n".join(lines).strip()


def format_download_status_lines(job: dict) -> list[str]:
    title = shorten(clean_download_name(job.get("name", "Unknown torrent")), 72)
    progress = float(job.get("progress", 0.0) or 0.0)
    total = int(job.get("total_length", 0) or 0)
    done = int(job.get("completed_length", 0) or 0)
    upload_done = int(job.get("upload_length", 0) or 0)
    state = str(job.get("status", "unknown")).replace("_", " ").title()
    bar = build_progress_bar(done, total, width=16)
    speed = (
        f"Down {human_speed(job.get('download_speed', 0))}  |  "
        f"Up {human_speed(job.get('upload_speed', 0))}"
    )
    peers = int(job.get("connections", 0) or 0)
    seeders = int(job.get("num_seeders", 0) or 0)
    provider = str(job.get("provider") or "aria2")

    if provider == "hentai-playlist":
        done_items = int(job.get("completed_items", 0) or 0)
        total_items = int(job.get("episode_count", 0) or 0)
        return [
            f"{ICON_DOWNLOAD} #{job['id']}  {title}",
            f"State: {state}  |  {progress:.1f}%",
            "Engine: yt-dlp playlist",
            f"Episodes: {done_items} / {total_items}",
            f"Current: {shorten(str(job.get('last_line') or 'Waiting...'), 100)}",
            "",
        ]

    lines = [
        f"{ICON_DOWNLOAD} #{job['id']}  {title}",
        f"State: {state}  |  {progress:.1f}%",
        f"Engine: {provider}",
    ]
    if total:
        lines.extend([
            bar,
            f"{ICON_BOX} {human_size(done)} / {human_size(total)}",
        ])
    elif done:
        lines.append(f"{ICON_BOX} {human_size(done)} downloaded")
    if provider == "aria2":
        lines.extend([
            f"{ICON_SPEED} {speed}",
            f"Peers: {peers}  |  Seeders: {seeders}",
        ])
    else:
        if job.get("download_speed"):
            lines.append(f"{ICON_SPEED} Down {human_speed(job.get('download_speed', 0))}")
        if job.get("last_line"):
            lines.append(f"Last: {shorten(str(job['last_line']), 120)}")
    if upload_done:
        lines.append(f"{ICON_UPLOAD} Uploaded: {human_size(upload_done)}")
    lines.extend([
        f"{ICON_CLOCK} ETA: {job.get('eta', 'Unknown')}",
        "",
    ])
    return lines


def build_status_controls_markup():
    active = [
        j
        for j in sorted(download_jobs.values(), key=lambda x: x["id"])
        if j["status"] in JOB_ACTIVE_STATES and j.get("status_visible", True)
    ]
    rows = []
    for job in active:
        jid = job["id"]
        if job.get("provider") in {"spotify", "manga", "yt-dlp", "hentai-playlist"}:
            rows.append([
                InlineKeyboardButton(f"{ICON_STOP} Cancel #{jid}", callback_data=f"job_cancel:{jid}"),
            ])
        else:
            if job.get("status") == "paused":
                toggle = InlineKeyboardButton(f"Resume #{jid}", callback_data=f"job_resume:{jid}")
            else:
                toggle = InlineKeyboardButton(f"Pause #{jid}", callback_data=f"job_pause:{jid}")
            rows.append([
                toggle,
                InlineKeyboardButton(f"{ICON_STOP} Cancel #{jid}", callback_data=f"job_cancel:{jid}"),
            ])
    return InlineKeyboardMarkup(rows) if rows else None


def build_live_dashboard_text(user_id: int = None):
    """Build live dashboard showing all jobs (active and inactive)."""
    u = user_id or 0
    
    if not download_jobs:
        return f"{ICON_QUEUE} {clean_emoji_prefix(get_lang(u, 'queue'))}\n\n{get_lang(u, 'no_jobs')}"

    lines = [f"{ICON_QUEUE} {clean_emoji_prefix(get_lang(u, 'delete_label'))}", ""]
    
    for j in sorted(download_jobs.values(), key=lambda x: x["id"], reverse=True):
        progress_bar = build_progress_bar(j.get('progress', 0) / 100 * j.get('total_length', 1), j.get('total_length', 1))
        lines.append(
            f"#{j['id']} [{j['status']}] {shorten(j['name'], 25)}\n{progress_bar}"
        )
    
    return "\n".join(lines)


def build_queue_text(user_id: int = None):
    u = user_id or 0
    if not download_jobs:
        return f"{ICON_QUEUE} {clean_emoji_prefix(get_lang(u, 'queue'))}\n\n{get_lang(u, 'no_jobs')}"

    lines = [f"{ICON_QUEUE} {clean_emoji_prefix(get_lang(u, 'queue'))}", ""]
    for j in sorted(download_jobs.values(), key=lambda x: x["id"], reverse=True):
        lines.append(
            f"#{j['id']} [{j['status']}] {j['name']} ({j.get('progress', 0.0):.1f}%)"
        )
    return "\n".join(lines)


# =========================================================
# File browser
# =========================================================

def list_dir(rel_path: str):
    full = safe_join(DOWNLOAD_DIR, rel_path)
    if not full.is_dir():
        raise NotADirectoryError(str(full))

    items = []
    for entry in full.iterdir():
        try:
            st = entry.stat()
            items.append({
                "name": entry.name,
                "rel_path": os.path.join(rel_path, entry.name) if rel_path else entry.name,
                "is_dir": entry.is_dir(),
                "size": st.st_size if entry.is_file() else 0,
                "mtime": st.st_mtime,
            })
        except FileNotFoundError:
            continue

    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return items


def file_info(rel_path: str):
    full = safe_join(DOWNLOAD_DIR, rel_path)
    if not full.exists():
        raise FileNotFoundError(str(full))
    st = full.stat()
    return {
        "name": full.name,
        "rel_path": rel_path,
        "is_dir": full.is_dir(),
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


def folder_info(rel_path: str):
    full = safe_join(DOWNLOAD_DIR, rel_path)
    if not full.is_dir():
        raise NotADirectoryError(str(full))

    total_size = 0
    file_count = 0
    folder_count = 0

    for root, dirs, files in os.walk(full):
        folder_count += len(dirs)
        file_count += len(files)
        for f in files:
            fp = Path(root) / f
            try:
                total_size += fp.stat().st_size
            except FileNotFoundError:
                pass

    st = full.stat()
    return {
        "name": rel_name(rel_path),
        "rel_path": rel_path,
        "folder_count": folder_count,
        "file_count": file_count,
        "total_size": total_size,
        "mtime": st.st_mtime,
    }


def build_files_text(rel_path: str, page: int = 0) -> str:
    entries = list_dir(rel_path)
    total = len(entries)
    pages = max(1, math.ceil(total / FILES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    shown_path = "/" + rel_path.lstrip("/") if rel_path else "/"

    lines = [
        f"{ICON_FOLDER} File Browser",
        f"{ICON_PIN} {shown_path}",
        f"{ICON_BOX} {total} items • Page {page + 1}/{pages}",
        "",
        "Tap a file or folder below."
    ]

    return "\n".join(lines)



def build_files_markup(rel_path: str, page: int = 0):
    """
    Fancy file browser layout:
    Row1: file/folder name
    Row2: actions (upload/delete/info)
    """
    entries = list_dir(rel_path)
    total = len(entries)
    pages = max(1, math.ceil(total / FILES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    shown = entries[page * FILES_PER_PAGE:(page + 1) * FILES_PER_PAGE]

    rows = []

    for item in shown:
        encoded = encode_path(item["rel_path"])
        icon = item_icon(item["name"], item["is_dir"])
        name_short = shorten(item["name"], 40)

        # NAME ROW
        if item["is_dir"]:
            rows.append([
                InlineKeyboardButton(
                    f"{icon} {name_short}",
                    callback_data=f"fb:dir:{page}:{encoded}"
                )
            ])
            rows.append([
                InlineKeyboardButton("📂 Open", callback_data=f"fb:dir:{page}:{encoded}"),
                InlineKeyboardButton("ℹ️ Info", callback_data=f"fb:dirinfo:{page}:{encoded}")
            ])
        else:
            rows.append([
                InlineKeyboardButton(
                    f"{icon} {name_short}",
                    callback_data=f"fb:file:{page}:{encoded}"
                )
            ])
            rows.append([
                InlineKeyboardButton("📤 Upload", callback_data=f"fb:upload_file_confirm:{page}:{encoded}"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"fb:delete_file_confirm:{page}:{encoded}"),
                InlineKeyboardButton("ℹ️ Info", callback_data=f"fb:file:{page}:{encoded}")
            ])

    # pagination
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⏮", callback_data=f"fb:list:0:{encode_path(rel_path)}"))
        nav.append(InlineKeyboardButton("◀", callback_data=f"fb:list:{page-1}:{encode_path(rel_path)}"))

    nav.append(InlineKeyboardButton(f"{page+1}/{pages}", callback_data="noop"))

    if page < pages-1:
        nav.append(InlineKeyboardButton("▶", callback_data=f"fb:list:{page+1}:{encode_path(rel_path)}"))
        nav.append(InlineKeyboardButton("⏭", callback_data=f"fb:list:{pages-1}:{encode_path(rel_path)}"))

    rows.append(nav)

    # navigation row
    rows.append([
        InlineKeyboardButton("⬆ Up", callback_data=f"fb:list:0:{encode_path(rel_parent(rel_path))}") if rel_path else InlineKeyboardButton("📁 Root", callback_data="fb:list:0:"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"fb:list:{page}:{encode_path(rel_path)}"),
    ])

    rows.append([
        InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(0, 'batch_upload'))}", callback_data=f"fb:batch:{page}:{encode_path(rel_path)}"),
        InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(0, 'batch_delete'))}", callback_data=f"fb:batchdel:{page}:{encode_path(rel_path)}"),
    ])
    rows.append([
        InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(0, 'delete_all'))}", callback_data=f"fb:deleteall_confirm:{page}:{encode_path(rel_path)}"),
    ])
    # Quick organizer for downloaded videos
    rows.append([
        InlineKeyboardButton(f"🧹 Organize videos", callback_data=f"fb:organize:{page}:{encode_path(rel_path)}"),
    ])

    return InlineKeyboardMarkup(rows)



def build_batch_select_text(rel_path: str, page: int = 0, user_id: int = None) -> str:
    """Text for batch file selection mode (upload or delete)."""
    entries = list_dir(rel_path)
    files = [e for e in entries if not e["is_dir"]]
    shown_path = "/" + rel_path.lstrip("/") if rel_path else "/"

    session = batch_select_sessions.get(user_id, {}) if user_id else {}
    mode = session.get("mode", "upload")
    selected_count = len(session.get("selected", set()))

    u = user_id or 0
    title = get_lang(u, "batch_delete") if mode == "delete" else get_lang(u, "batch_upload")

    lines = [
        title,
        f"{ICON_PIN} {shown_path}",
        f"Files: {len(files)} • Selected: {selected_count}",
        "",
        get_lang(u, "select_files"),
    ]

    return "\n".join(lines)


def build_batch_select_markup(rel_path: str, user_id: int, page: int = 0):
    """Keyboard for batch file selection."""
    entries = list_dir(rel_path)
    files = [e for e in entries if not e["is_dir"]]

    session = batch_select_sessions.get(user_id, {})
    mode = session.get("mode", "upload")
    selected = session.get("selected", set())
    
    total = len(files)
    pages = max(1, math.ceil(total / FILES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    shown = files[page * FILES_PER_PAGE:(page + 1) * FILES_PER_PAGE]
    
    rows = []
    
    for idx, item in enumerate(shown, start=1):
        encoded = encode_path(item["rel_path"])
        is_selected = item["rel_path"] in selected
        checkbox = "✅" if is_selected else "⬜"
        size_text = human_size(item["size"])
        label = f"{checkbox} {item['name']}"
        
        rows.append([
            InlineKeyboardButton(
                f"{idx}. {shorten(label, 32)}",
                callback_data=f"fb:bselect:{encoded}"
            ),
            InlineKeyboardButton(
                shorten(size_text, 8),
                callback_data=f"fb:bselect:{encoded}"
            )
        ])
    
    pager = []
    if page > 0:
        pager.append(
            InlineKeyboardButton(
                f"{ICON_BACK} First",
                callback_data=f"fb:blist:0:{encode_path(rel_path)}"
            )
        )
    if page > 0:
        pager.append(
            InlineKeyboardButton(
                f"{ICON_BACK} Prev",
                callback_data=f"fb:blist:{page - 1}:{encode_path(rel_path)}"
            )
        )
    pager.append(
        InlineKeyboardButton(
            f"{page + 1}/{pages}",
            callback_data=f"fb:blist:{page}:{encode_path(rel_path)}"
        )
    )
    if page < pages - 1:
        pager.append(
            InlineKeyboardButton(
                f"Next {ICON_NEXT}",
                callback_data=f"fb:blist:{page + 1}:{encode_path(rel_path)}"
            )
        )
    if page < pages - 1:
        pager.append(
            InlineKeyboardButton(
                f"Last {ICON_NEXT}",
                callback_data=f"fb:blist:{pages - 1}:{encode_path(rel_path)}"
            )
        )
    rows.append(pager)
    
    selected_count = len(selected)
    u = user_id or 0
    if mode == "delete":
        action_label = (
            get_lang(u, "batch_delete_files").format(selected_count)
            if selected_count > 0
            else get_lang(u, "select_at_least")
        )
        action_cb = "fb:bdelete_confirm" if selected_count > 0 else "fb:bupload_empty"
    else:
        action_label = (
            get_lang(u, "upload_files").format(selected_count)
            if selected_count > 0
            else get_lang(u, "select_at_least")
        )
        action_cb = "fb:bupload" if selected_count > 0 else "fb:bupload_empty"
    rows.append([InlineKeyboardButton(action_label, callback_data=action_cb)])
    
    rows.append([
        InlineKeyboardButton(f"{ICON_BACK} Cancel", callback_data=f"fb:list:{page}:{encode_path(rel_path)}"),
    ])
    
    return InlineKeyboardMarkup(rows)


def build_file_details_text(rel_path: str) -> str:
    info = file_info(rel_path)
    dt = datetime.fromtimestamp(info["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
    shown_path = "/" + rel_path.lstrip("/")
    icon = item_icon(info["name"], False)
    return (
        f"{icon} File Details\n\n"
        f"Name: {info['name']}\n"
        f"{ICON_PIN} Path: {shown_path}\n"
        f"{ICON_BOX} Size: {human_size(info['size'])}\n"
        f"{ICON_CLOCK} Modified: {dt}"
    )


def build_folder_details_text(rel_path: str) -> str:
    info = folder_info(rel_path)
    shown_path = "/" + rel_path.lstrip("/") if rel_path else "/"
    dt = datetime.fromtimestamp(info["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"{ICON_FOLDER} Folder Details\n\n"
        f"Name: {info['name']}\n"
        f"{ICON_PIN} Path: {shown_path}\n"
        f"Subfolders: {info['folder_count']}\n"
        f"Files: {info['file_count']}\n"
        f"{ICON_BOX} Total size: {human_size(info['total_size'])}\n"
        f"{ICON_CLOCK} Modified: {dt}"
    )


def build_file_details_markup(rel_path: str, page: int = 0):
    encoded = encode_path(rel_path)
    parent = encode_path(rel_parent(rel_path))
    buttons = [
        [InlineKeyboardButton(f"{ICON_UPLOAD} Upload File", callback_data=f"fb:send_confirm:{page}:{encoded}")],
    ]
    # Check if video file to add conversion and thumbnail buttons
    full = safe_join(DOWNLOAD_DIR, rel_path)
    if full.is_file() and is_video_file(str(full)):
        buttons.append([InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(0, 'convert'))}", callback_data=f"fb:conv_menu:{page}:{encoded}")])
        buttons.append([InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(0, 'send_thumbnail'))}", callback_data=f"fb:thumb_send:{page}:{encoded}")])
    buttons.append([InlineKeyboardButton(f"{ICON_DELETE} Delete", callback_data=f"fb:delete_confirm:{page}:{encoded}")])
    buttons.append([
        InlineKeyboardButton(f"{ICON_BACK} Back", callback_data=f"fb:list:{page}:{parent}"),
        InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")
    ])
    return InlineKeyboardMarkup(buttons)


def build_folder_details_markup(rel_path: str, page: int = 0):
    encoded = encode_path(rel_path)
    parent = encode_path(rel_parent(rel_path))
    buttons = [
        [InlineKeyboardButton(f"{ICON_FOLDER} Open Folder", callback_data=f"fb:list:0:{encoded}")],
        [InlineKeyboardButton(f"{ICON_UPLOAD} Upload All Files", callback_data=f"fb:send_folder_confirm:{page}:{encoded}")],
        [InlineKeyboardButton(f"{ICON_DELETE} Delete Folder", callback_data=f"fb:delete_confirm:{page}:{encoded}")],
        [
            InlineKeyboardButton(f"{ICON_BACK} Back", callback_data=f"fb:list:{page}:{parent}"),
            InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")
        ],
    ]
    full = safe_join(DOWNLOAD_DIR, rel_path)
    if is_manga_gallery_folder(full):
        buttons.insert(
            2,
            [InlineKeyboardButton("Convert to PDF", callback_data=f"fb:manga_pdf:{page}:{encoded}")],
        )
    return InlineKeyboardMarkup(buttons)


def is_manga_gallery_folder(folder: Path) -> bool:
    try:
        folder.relative_to(MANGA_DIR)
    except ValueError:
        return False
    return folder.is_dir() and bool(list_manga_images(folder))


def build_delete_confirm_text(rel_path: str) -> str:
    full = safe_join(DOWNLOAD_DIR, rel_path)
    shown_path = "/" + rel_path.lstrip("/") if rel_path else "/"
    name = rel_name(rel_path)

    if full.is_dir():
        info = folder_info(rel_path)
        return (
            f"{ICON_WARN} Confirm Delete\n\n"
            f"Type: Folder\n"
            f"Name: {name}\n"
            f"{ICON_PIN} Path: {shown_path}\n"
            f"Subfolders: {info['folder_count']}\n"
            f"Files: {info['file_count']}\n"
            f"{ICON_BOX} Total size: {human_size(info['total_size'])}\n\n"
            f"{ICON_WARN} Warning: this deletes everything inside."
        )

    info = file_info(rel_path)
    return (
        f"{ICON_WARN} Confirm Delete\n\n"
        f"Type: File\n"
        f"Name: {name}\n"
        f"{ICON_PIN} Path: {shown_path}\n"
        f"{ICON_BOX} Size: {human_size(info['size'])}\n\n"
        f"{ICON_WARN} Warning: this file will be permanently deleted."
    )


def build_delete_confirm_markup(rel_path: str, page: int = 0):
    encoded = encode_path(rel_path)
    parent = encode_path(rel_parent(rel_path))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ICON_DELETE} Yes, Delete", callback_data=f"fb:delete_yes:{page}:{encoded}")],
        [
            InlineKeyboardButton(f"{ICON_BACK} Cancel", callback_data=f"fb:list:{page}:{parent}"),
            InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")
        ],
    ])


def delete_path(rel_path: str):
    full = safe_join(DOWNLOAD_DIR, rel_path)
    if not full.exists():
        raise FileNotFoundError(str(full))
    if full.is_dir():
        shutil.rmtree(full)
        return "folder"
    full.unlink()
    return "file"


def delete_all_in_directory(rel_path: str) -> tuple:
    """Delete every file and folder directly inside rel_path."""
    files_deleted = 0
    folders_deleted = 0
    errors = []
    for item in list_dir(rel_path):
        try:
            kind = delete_path(item["rel_path"])
            if kind == "file":
                files_deleted += 1
            else:
                folders_deleted += 1
        except Exception as e:
            errors.append(f"{item['name']}: {e}")
    return files_deleted, folders_deleted, errors


def delete_paths_batch(rel_paths: list) -> tuple:
    """Delete multiple paths. Returns (deleted_count, errors)."""
    deleted = 0
    errors = []
    for rel in rel_paths:
        try:
            delete_path(rel)
            deleted += 1
        except Exception as e:
            errors.append(f"{rel}: {e}")
    return deleted, errors

def is_duplicate_name(name: str):
    for root, _, files in os.walk(DOWNLOAD_DIR):
        if name in files:
            return True
    return False


# =========================================================
# Upload helpers
# =========================================================

def get_file_mime_type(file_path: str) -> str:
    """Get MIME type for a file."""
    mime, _ = mimetypes.guess_type(file_path)
    return mime or "application/octet-stream"


def is_video_file(file_path: str) -> bool:
    """Check if file is a video."""
    video_exts = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".m3u8"}
    return Path(file_path).suffix.lower() in video_exts


def is_audio_file(file_path: str) -> bool:
    """Check if file is audio."""
    audio_exts = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".opus", ".aiff"}
    return Path(file_path).suffix.lower() in audio_exts


def is_image_file(file_path: str) -> bool:
    """Check if file is an image."""
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg"}
    return Path(file_path).suffix.lower() in image_exts


def get_send_method(file_path: str) -> str:
    """Determine the best send method: document, video, audio, or photo."""
    if is_video_file(file_path):
        return "video"
    if is_audio_file(file_path):
        return "audio"
    if is_image_file(file_path):
        return "photo"
    return "document"


def build_upload_caption(rel_path: str) -> str:
    """Build a formatted caption with file metadata."""
    full = safe_join(DOWNLOAD_DIR, rel_path)
    if not full.exists():
        return f"Uploaded: {rel_path}"
    
    st = full.stat()
    size = human_size(st.st_size)
    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    name = full.name
    path = "/" + rel_path.lstrip("/")
    
    return (
        f"📄 {name}\n\n"
        f"📊 Size: {size}\n"
        f"📅 Modified: {mtime}\n"
        f"📍 Path: {path}"
    )


def build_progress_bar(current: int, total: int, width: int = 20) -> str:
    """Build a visual progress bar."""
    if total <= 0:
        pct = 0
        filled = 0
    else:
        ratio = max(0.0, min(1.0, current / total))
        pct = int(ratio * 100)
        filled = int(ratio * width)

    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {pct}%"


def split_file_into_chunks(file_path: str, chunk_size: int = 2 * 1024 * 1024 * 1024) -> list:
    """
    Split a file into chunks if it exceeds chunk_size.
    Returns a list of (chunk_path, chunk_index, total_chunks) tuples.
    """
    full = safe_join(DOWNLOAD_DIR, file_path)
    file_size = full.stat().st_size
    
    if file_size <= chunk_size:
        return [(str(full), 1, 1)]
    
    chunk_dir = full.parent / f"{full.stem}_chunks"
    chunk_dir.mkdir(exist_ok=True)
    
    chunks = []
    chunk_num = 1
    total_chunks = math.ceil(file_size / chunk_size)
    
    with open(full, "rb") as src:
        while True:
            chunk_data = src.read(chunk_size)
            if not chunk_data:
                break
            
            chunk_path = chunk_dir / f"{full.stem}_part_{chunk_num:03d}{full.suffix}"
            with open(chunk_path, "wb") as dst:
                dst.write(chunk_data)
            
            chunks.append((str(chunk_path), chunk_num, total_chunks))
            chunk_num += 1
    
    return chunks


def get_video_metadata(file_path: str):
    """Extract video width, height and duration using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height:format=duration",
                "-of", "default=noprint_wrappers=1:nokey=0",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        width = None
        height = None
        duration = None

        for line in result.stdout.splitlines():
            if line.startswith("width="):
                width = int(float(line.split("=", 1)[1].strip()))
            elif line.startswith("height="):
                height = int(float(line.split("=", 1)[1].strip()))
            elif line.startswith("duration="):
                try:
                    duration = int(float(line.split("=", 1)[1].strip()))
                except Exception:
                    duration = None

        return {
            "width": width,
            "height": height,
            "duration": duration,
        }

    except Exception:
        return {
            "width": None,
            "height": None,
            "duration": None,
        }


def cleanup_orphan_thumbnails(directory: Path = None) -> None:
    """Remove stale generated thumbnail files."""
    root = directory or DOWNLOAD_DIR
    try:
        for thumb in root.rglob(".thumb_*.jpg"):
            try:
                if thumb.is_file():
                    thumb.unlink()
            except Exception:
                pass
    except Exception:
        pass


def generate_thumbnail(file_path: str, output_size: tuple = (320, 180)) -> str:
    """Generate a contact sheet thumbnail for video using new thumbnail_generate module.
    Returns path to thumbnail or empty string."""
    if not HAS_PIL:
        return ""

    path = Path(file_path)
    if path.is_absolute():
        full = path.resolve()
        base_abs = DOWNLOAD_DIR.resolve()
        if not (full == base_abs or str(full).startswith(str(base_abs) + os.sep)):
            return ""
    else:
        full = safe_join(DOWNLOAD_DIR, file_path)

    try:
        if is_video_file(str(full)):
            # Use new thumbnail_generate module for video contact sheets
            thumb_path = full.parent / f".thumb_{full.stem}.jpg"
            
            # Generate contact sheet using the new module
            generate_contact_sheet(str(full), str(thumb_path))
            
            if thumb_path.exists():
                return str(thumb_path)
        elif is_image_file(str(full)):
            # For images, create a simple thumbnail
            img = Image.open(full)
            resample_filter = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.LANCZOS)
            img.thumbnail(output_size, resample_filter)
            thumb_path = full.parent / f".thumb_{full.stem}.jpg"
            img.save(thumb_path, "JPEG", quality=80)
            return str(thumb_path)
    except Exception as e:
        logger.debug(f"Error generating thumbnail for {file_path}: {e}")
    
    return ""


# =========================================================
# Video conversion helpers (new)
# =========================================================

def get_video_resolution(file_path: str) -> tuple:
    """Return (width, height) of video or (None, None) on error."""
    try:
        result = subprocess.run(
            [FFMPEG_BIN, "-i", file_path],
            stderr=subprocess.PIPE, text=True, timeout=15
        )
        for line in result.stderr.splitlines():
            if "Video:" in line and "," in line:
                parts = line.split(",")
                for p in parts:
                    if "x" in p and p.strip()[0].isdigit():
                        w, h = p.strip().split("x", 1)
                        return int(w), int(h.split()[0])
    except Exception:
        pass
    return None, None


async def convert_video_quality(input_path: str, output_path: str, target_res: str, progress_callback=None):
    """
    Re‑encode video to given resolution (e.g. '720p').
    Uses libx264, constant frame rate, CRF 23, preset 'medium'.
    Progress parsing from ffmpeg stderr (time=...).
    """
    resolution_map = {
        "1080p": 1080,
        "720p": 720,
        "480p": 480,
        "360p": 360,
    }
    target_h = resolution_map.get(target_res, 720)
    scale_filter = f"scale=-2:{target_h}"
    cmd = [
        FFMPEG_BIN,
        "-i", input_path,
        "-vf", scale_filter,
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "medium",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-y",
        output_path
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    duration = None
    # try to get duration from original file
    probe = subprocess.run(
        [FFMPEG_BIN, "-i", input_path],
        stderr=subprocess.PIPE, text=True, timeout=10
    )
    for line in probe.stderr.splitlines():
        if "Duration:" in line:
            time_str = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = map(float, time_str.split(":"))
            duration = h * 3600 + m * 60 + s
            break

    last_update = 0
    while True:
        line = await process.stderr.readline()
        if not line:
            break
        line = line.decode("utf-8", errors="ignore").strip()
        if progress_callback and "time=" in line and duration:
            time_part = line.split("time=")[1].split()[0]
            try:
                h, m, s = map(float, time_part.split(":"))
                current = h * 3600 + m * 60 + s
                pct = min(100, int((current / duration) * 100))
                now = time.time()
                if now - last_update > 1.0:
                    last_update = now
                    await progress_callback(pct)
            except (ValueError, IndexError):
                pass

    await process.wait()
    if process.returncode != 0:
        raise RuntimeError("ffmpeg conversion failed")


async def send_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE, rel_path: str):
    """
    Generate a contact sheet thumbnail grid from a video using thumbnail_generate.py.
    Creates a 4x8 grid (32 frames total) from evenly spaced points in the video.
    """
    full = safe_join(DOWNLOAD_DIR, rel_path)

    if not full.exists() or not is_video_file(str(full)):
        await update.callback_query.answer("Not a valid video file", show_alert=True)
        return

    tmp_dir = Path(tempfile.gettempdir()) / f"thumb_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Generate contact sheet thumbnail grid using thumbnail_generate.py
        output_path = tmp_dir / "contact_sheet.jpg"
        
        try:
            generate_contact_sheet(str(full), str(output_path))
        except Exception as e:
            await update.callback_query.answer(f"Thumbnail generation failed: {str(e)}", show_alert=True)
            return

        if not output_path.exists():
            await update.callback_query.answer("Thumbnail generation failed", show_alert=True)
            return

        # Send the contact sheet as a photo
        with open(output_path, "rb") as img:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=img,
                caption=f"📸 Thumbnail Grid: {full.name}"
            )

    except Exception as e:
        await update.callback_query.answer(f"Error: {str(e)}", show_alert=True)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# =========================================================
# yt-dlp video downloader
# =========================================================

def is_video_url(text: str) -> bool:
    text = extract_http_url(text).lower()
    return is_supported_video_url(text)


def build_hentai_playlist_prompt_text(playlist: HentaiPlaylist) -> str:
    return (
        f"{ICON_DOWNLOAD} Hentai playlist detected\n\n"
        f"Site: {playlist.site}\n"
        f"Title:\n{shorten(clean_download_name(playlist.title), 100)}\n"
        f"Episodes: {len(playlist.urls)}\n\n"
        f"Folder:\n{HENTAI_VIDEO_DIR / video_platform_slug(playlist.urls[0])}"
    )


def build_hentai_playlist_started_text(job: dict) -> str:
    return (
        f"{ICON_DOWNLOAD} Playlist download started\n\n"
        f"Job: #{job['id']}\n"
        f"Site: {job.get('platform', 'Hentai')}\n"
        f"Title:\n{shorten(clean_download_name(job.get('name', 'Playlist')), 100)}\n"
        f"Episodes: {job.get('episode_count', 0)}"
    )


def extract_spotify_url(text: str) -> str:
    match = re.search(
        r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?"
        r"(?:track|album|playlist|artist|episode|show)/[A-Za-z0-9]+(?:\?[^\s]+)?",
        text.strip(),
        re.IGNORECASE,
    )
    return match.group(0) if match else text.strip()


def build_spotify_prompt_text(url: str) -> str:
    return (
        f"{ICON_AUDIO} Spotify link detected\n\n"
        "Download this with spotDL?\n\n"
        f"Folder:\n{SPOTIFY_DIR}\n\n"
        f"Link:\n{shorten(url, 160)}"
    )


def build_spotify_started_text(job: dict) -> str:
    return (
        f"{ICON_AUDIO} Spotify download started\n\n"
        f"Job: #{job['id']}\n"
        "Engine: spotDL\n"
        f"Folder:\n{SPOTIFY_DIR}"
    )


def build_spotify_completed_text(job: dict) -> str:
    title = shorten(clean_download_name(job.get("name", "Spotify download")), 90)
    count = int(job.get("artifact_count", 0) or 0)
    lines = [
        f"{ICON_OK} Spotify download completed",
        "",
        f"Job: #{job['id']}",
        f"Title:\n{title}",
    ]
    if count:
        lines.append(f"Files: {count}")
    lines.append(f"Folder:\n{SPOTIFY_DIR}")
    return "\n".join(lines)


def build_manga_prompt_text(url: str) -> str:
    return (
        f"{ICON_IMAGE} Manga/gallery link detected\n\n"
        "Download this gallery?\n\n"
        f"Folder:\n{MANGA_DIR}\n\n"
        f"Link:\n{shorten(url, 160)}"
    )


def build_manga_started_text(job: dict) -> str:
    return (
        f"{ICON_IMAGE} Manga download started\n\n"
        f"Job: #{job['id']}\n"
        "Engine: manga gallery downloader\n"
        f"Folder:\n{MANGA_DIR}"
    )


def build_manga_completed_text(job: dict) -> str:
    lines = [
        f"{ICON_OK} Manga download completed",
        "",
        f"Job: #{job['id']}",
        f"Title:\n{shorten(clean_download_name(job.get('name', 'Manga gallery')), 90)}",
        f"Images: {job.get('image_count', 0)}",
        f"Folder:\n{job.get('folder', MANGA_DIR)}",
    ]
    if job.get("pdf_path"):
        lines.extend(["", f"PDF:\n{job['pdf_path']}"])
    return "\n".join(lines)


def build_manga_settings_text(user_id: int) -> str:
    settings = get_user_settings(user_id)
    return (
        f"{ICON_IMAGE} Manga Settings\n\n"
        f"Auto convert manga to PDF: {'ON' if settings.get('manga_auto_convert_pdf') else 'OFF'}\n"
        f"Remove images after conversion: {'ON' if settings.get('manga_remove_images_after_conversion') else 'OFF'}\n\n"
        f"Downloaded galleries go to:\n{MANGA_DIR}\n\n"
        "PDF files are created in the main Download folder."
    )


def build_manga_settings_markup(user_id: int) -> InlineKeyboardMarkup:
    settings = get_user_settings(user_id)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"Auto convert PDF: {'ON' if settings.get('manga_auto_convert_pdf') else 'OFF'}",
                callback_data="manga_setting:auto_convert",
            )
        ],
        [
            InlineKeyboardButton(
                "Remove images after PDF: "
                f"{'ON' if settings.get('manga_remove_images_after_conversion') else 'OFF'}",
                callback_data="manga_setting:remove_images",
            )
        ],
    ])


async def convert_manga_folder_to_pdf_job(folder: Path, user_id: int) -> Path:
    settings = get_user_settings(user_id)
    remove_images = bool(settings.get("manga_remove_images_after_conversion"))
    loop = asyncio.get_running_loop()
    pdf_path = await loop.run_in_executor(
        None,
        lambda: convert_images_to_pdf(
            folder,
            DOWNLOAD_DIR,
            remove_images=remove_images,
            title=folder.name,
        ),
    )
    if remove_images:
        remove_manga_folder_if_empty(folder)
    return pdf_path


async def start_manga_download(app: Application, chat_id: int, url: str, user_id: int = None):
    global job_counter, download_jobs

    async with jobs_lock:
        job_counter += 1
        job_id = job_counter

    job = {
        "id": job_id,
        "provider": "manga",
        "name": "Manga gallery",
        "url": url,
        "chat_id": chat_id,
        "user_id": user_id or 0,
        "pid": None,
        "process": None,
        "status": "starting",
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "upload_speed": 0,
        "eta": "Unknown",
        "started_at": now_ts(),
        "finished_at": None,
        "last_line": "Fetching gallery images...",
        "image_count": 0,
        "folder": str(MANGA_DIR),
        "pdf_path": "",
    }
    download_jobs[job_id] = job

    async def run_job():
        try:
            result = await download_manga_gallery(url, MANGA_DIR)
            if job.get("status") == "cancelled":
                return
            job["status"] = "processing"
            job["name"] = result.title
            job["progress"] = 90.0
            job["image_count"] = len(result.images)
            job["folder"] = str(result.folder)
            job["last_line"] = "Images downloaded"
            settings = get_user_settings(user_id or 0)
            if settings.get("manga_auto_convert_pdf"):
                job["last_line"] = "Converting images to PDF..."
                pdf_path = await convert_manga_folder_to_pdf_job(result.folder, user_id or 0)
                job["pdf_path"] = str(pdf_path)
            job["status"] = "completed"
            job["progress"] = 100.0
            job["finished_at"] = now_ts()
            job["last_line"] = "Completed"
            await maybe_auto_update_status_message(app, job, force=True)
            await app.bot.send_message(
                chat_id=chat_id,
                text=build_manga_completed_text(job),
                reply_markup=build_reply_menu(user_id),
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.exception("Manga download failed")
            job["status"] = "failed"
            job["finished_at"] = now_ts()
            job["last_line"] = str(e)
            await maybe_auto_update_status_message(app, job, force=True)
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{ICON_FAIL} Manga download failed.\n\n"
                    f"Job: #{job_id}\n"
                    f"Reason:\n{shorten(str(e), 900)}"
                ),
                reply_markup=build_reply_menu(user_id),
                disable_web_page_preview=True,
            )

    asyncio.create_task(run_job())
    return job


async def start_spotify_download(app: Application, chat_id: int, url: str, user_id: int = None):
    global job_counter, download_jobs

    async with jobs_lock:
        job_counter += 1
        job_id = job_counter

    job = {
        "id": job_id,
        "provider": "spotify",
        "name": "Spotify download",
        "url": url,
        "chat_id": chat_id,
        "user_id": user_id or 0,
        "pid": None,
        "process": None,
        "status": "starting",
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "upload_speed": 0,
        "eta": "Unknown",
        "started_at": now_ts(),
        "finished_at": None,
        "last_line": "Waiting for spotDL...",
        "artifact_count": 0,
    }
    download_jobs[job_id] = job

    def set_process(process):
        job["process"] = process
        job["pid"] = getattr(process, "pid", None)

    def update_progress(line: str, percent: float | None):
        job["status"] = "downloading"
        job["last_line"] = shorten(line, 220)
        if percent is not None:
            job["progress"] = percent

    async def run_job():
        provider = SpotifyDownloader(spotdl_bin=SPOTDL_BIN, ffmpeg_bin=FFMPEG_BIN)
        try:
            result = await provider.download(
                DownloadRequest(
                    url=url,
                    destination=SPOTIFY_DIR,
                    options={
                        "process_callback": set_process,
                        "progress_callback": update_progress,
                    },
                )
            )
            if job.get("status") == "cancelled":
                return
            total_size = sum(
                artifact.size_bytes or 0
                for artifact in result.artifacts
                if artifact.media_type == "audio"
            )
            job["status"] = "completed"
            job["name"] = result.title
            job["progress"] = 100.0
            job["completed_length"] = total_size
            job["total_length"] = total_size
            job["artifact_count"] = len(result.artifacts)
            job["finished_at"] = now_ts()
            job["last_line"] = "Completed"
            await maybe_auto_update_status_message(app, job, force=True)
            await app.bot.send_message(
                chat_id=chat_id,
                text=build_spotify_completed_text(job),
                reply_markup=build_reply_menu(user_id),
                disable_web_page_preview=True,
            )
        except Exception as e:
            if job.get("status") == "cancelled":
                return
            logger.exception("spotDL download failed")
            job["status"] = "failed"
            job["finished_at"] = now_ts()
            job["last_line"] = str(e)
            await maybe_auto_update_status_message(app, job, force=True)
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{ICON_FAIL} Spotify download failed.\n\n"
                    f"Job: #{job_id}\n"
                    f"Reason:\n{shorten(str(e), 900)}"
                ),
                reply_markup=build_reply_menu(user_id),
                disable_web_page_preview=True,
            )
        finally:
            job["process"] = None

    asyncio.create_task(run_job())
    return job


async def start_ytdlp_download(
    app: Application,
    chat_id: int,
    url: str,
    audio_only: bool = False,
    user_id: int = None,
    run_in_background: bool = False,
    notify: bool = True,
):
    global job_counter, download_jobs

    async with jobs_lock:
        job_counter += 1
        job_id = job_counter

    is_hentai = is_hentai_video_url(url)
    is_adult = is_adult_video_url(url)
    platform = video_platform_label(url)
    if is_hentai:
        output_dir = HENTAI_VIDEO_DIR / video_platform_slug(url)
    elif is_adult:
        output_dir = ADULT_VIDEO_DIR / video_platform_slug(url)
    else:
        output_dir = DOWNLOAD_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    job = {
        "id": job_id,
        "name": "Fetching video info...",
        "url": url,
        "chat_id": chat_id,
        "user_id": user_id or 0,
        "provider": "yt-dlp",
        "source_type": "hentai_video" if is_hentai else "adult_video" if is_adult else "video",
        "platform": platform,
        "pid": None,
        "process": None,
        "status": "starting",
        "status_visible": notify,
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "upload_speed": 0,
        "eta": "Unknown",
        "started_at": now_ts(),
        "finished_at": None,
        "last_line": "",
        "folder": str(output_dir),
    }

    download_jobs[job_id] = job

    loop = asyncio.get_running_loop()

    def progress_hook(d):
        try:
            status = d.get("status")

            if status == "downloading":
                total = (
                    d.get("total_bytes")
                    or d.get("total_bytes_estimate")
                    or 0
                )

                downloaded = d.get("downloaded_bytes", 0)
                speed = d.get("speed") or 0
                eta = d.get("eta")

                pct = (downloaded / total * 100) if total else 0

                job["status"] = "downloading"
                job["progress"] = pct
                job["completed_length"] = downloaded
                job["total_length"] = total
                job["download_speed"] = speed
                job["eta"] = format_eta(eta)

            elif status == "finished":
                job["status"] = "processing"
                job["progress"] = 100.0

        except Exception as e:
            logger.exception(f"yt-dlp progress hook error: {e}")

    async def refresh_ytdlp_status():
        if not notify:
            return
        while job["status"] in JOB_ACTIVE_STATES:
            await maybe_auto_update_status_message(app, job)
            await asyncio.sleep(1)

    def run_download():
        ydl_opts = {
            "outtmpl": str(output_dir / "%(title).200B [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "concurrent_fragment_downloads": 4,
            "retries": 10,
            "fragment_retries": 10,
            "extractor_retries": 3,
            "file_access_retries": 3,
            "socket_timeout": 30,
            "continuedl": True,
            "part": True,
            "windowsfilenames": False,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        }

        if YTDLP_COOKIES_FILE and Path(YTDLP_COOKIES_FILE).exists():
            ydl_opts["cookiefile"] = YTDLP_COOKIES_FILE
        if YTDLP_PROXY:
            ydl_opts["proxy"] = YTDLP_PROXY

        if audio_only:
            ydl_opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
            })
        else:
            ydl_opts.update({
                "format": "bv*+ba/b",
                "merge_output_format": "mp4",
            })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

            if info is None:
                raise RuntimeError("Failed to extract video info")

            title = info.get("title") or "Unknown Video"

            filepath = None

            requested = info.get("requested_downloads") or []
            if requested:
                filepath = requested[0].get("filepath")

            if not filepath:
                filepath = info.get("_filename")

            if not filepath:
                filepath = ydl.prepare_filename(info)

            if audio_only:
                base_path = os.path.splitext(filepath)[0]
                mp3_path = f"{base_path}.mp3"
                if os.path.exists(mp3_path):
                    filepath = mp3_path

            return title, filepath

    async def finish_download():
        refresh_task = asyncio.create_task(refresh_ytdlp_status())
        try:
            title, filepath = await loop.run_in_executor(None, run_download)
            if job.get("status") == "cancelled":
                if notify:
                    await maybe_auto_update_status_message(app, job, force=True)
                return

            job["status"] = "completed"
            job["name"] = title
            job["progress"] = 100.0
            if filepath and os.path.exists(filepath):
                size = os.path.getsize(filepath)
                job["completed_length"] = size
                job["total_length"] = size
            job["download_speed"] = 0
            job["finished_at"] = now_ts()
            if notify:
                await maybe_auto_update_status_message(app, job, force=True)

            if notify:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"{ICON_OK} {'MP3' if audio_only else 'Video'} download completed.\n\n"
                        f"Job: #{job_id}\n"
                        f"Platform: {platform}\n"
                        f"Title:\n{shorten(title, 140)}\n"
                        f"Folder:\n{output_dir}"
                    ),
                    reply_markup=build_reply_menu(user_id),
                    disable_web_page_preview=True,
                )

        except Exception as e:
            logger.exception("yt-dlp download failed")

            job["status"] = "failed"
            job["finished_at"] = now_ts()
            job["last_line"] = str(e)
            if notify:
                await maybe_auto_update_status_message(app, job, force=True)

            if notify:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"{ICON_FAIL} Video download failed.\n\n"
                        f"Job: #{job_id}\n"
                        f"Platform: {platform}\n"
                        f"Reason:\n{shorten(str(e), 900)}"
                    ),
                    reply_markup=build_reply_menu(user_id),
                    disable_web_page_preview=True,
                )
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass

    if notify:
        await update_status_message(app, chat_id, user_id)
    if run_in_background:
        asyncio.create_task(finish_download())
    else:
        await finish_download()

    return job


async def start_hentai_playlist_download(
    app: Application,
    chat_id: int,
    playlist: HentaiPlaylist,
    user_id: int = None,
):
    global job_counter, download_jobs

    async with jobs_lock:
        job_counter += 1
        job_id = job_counter

    episode_count = len(playlist.urls)
    job = {
        "id": job_id,
        "name": playlist.title,
        "url": playlist.urls[0] if playlist.urls else "",
        "chat_id": chat_id,
        "user_id": user_id or 0,
        "provider": "hentai-playlist",
        "source_type": "hentai_playlist",
        "platform": playlist.site,
        "pid": None,
        "process": None,
        "status": "starting",
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "upload_speed": 0,
        "eta": "Unknown",
        "started_at": now_ts(),
        "finished_at": None,
        "last_line": "Preparing playlist...",
        "episode_count": episode_count,
        "completed_items": 0,
        "folder": str(HENTAI_VIDEO_DIR),
    }
    download_jobs[job_id] = job

    async def run_playlist():
        try:
            job["status"] = "downloading"
            await maybe_auto_update_status_message(app, job, force=True)
            for index, episode_url in enumerate(playlist.urls, start=1):
                if job.get("status") == "cancelled":
                    return
                job["last_line"] = f"Episode {index}/{episode_count}"
                job["progress"] = ((index - 1) / episode_count * 100) if episode_count else 0.0
                await maybe_auto_update_status_message(app, job, force=True)
                child = await start_ytdlp_download(
                    app,
                    chat_id,
                    episode_url,
                    audio_only=False,
                    user_id=user_id,
                    notify=False,
                )
                if child.get("status") == "failed":
                    job["last_line"] = f"Episode {index} failed: {child.get('last_line', 'Unknown')}"
                    raise RuntimeError(job["last_line"])
                job["completed_items"] = index
                job["progress"] = (index / episode_count * 100) if episode_count else 100.0

            job["status"] = "completed"
            job["progress"] = 100.0
            job["finished_at"] = now_ts()
            job["last_line"] = "Completed"
            await maybe_auto_update_status_message(app, job, force=True)
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{ICON_OK} Playlist download completed\n\n"
                    f"Job: #{job_id}\n"
                    f"Title:\n{shorten(clean_download_name(playlist.title), 100)}\n"
                    f"Episodes: {episode_count}\n"
                    f"Folder:\n{HENTAI_VIDEO_DIR}"
                ),
                reply_markup=build_reply_menu(user_id),
                disable_web_page_preview=True,
            )
        except Exception as exc:
            if job.get("status") == "cancelled":
                return
            logger.exception("Hentai playlist download failed")
            job["status"] = "failed"
            job["finished_at"] = now_ts()
            job["last_line"] = str(exc)
            await maybe_auto_update_status_message(app, job, force=True)
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"{ICON_FAIL} Playlist download failed.\n\n"
                    f"Job: #{job_id}\n"
                    f"Reason:\n{shorten(str(exc), 900)}"
                ),
                reply_markup=build_reply_menu(user_id),
                disable_web_page_preview=True,
            )

    await update_status_message(app, chat_id, user_id)
    asyncio.create_task(run_playlist())
    return job


async def handle_ytdlp_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle yt-dlp download format selection."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    pending = pending_ytdlp_requests.pop(user_id, None)

    if not pending:
        await query.edit_message_text(
            f"{ICON_WARN} No pending yt-dlp request found."
        )
        return

    audio_only = query.data == "ytdlp_mp3"

    await query.edit_message_text(
        f"{ICON_DOWNLOAD} Starting {'MP3' if audio_only else 'video'} download..."
    )

    await start_ytdlp_download(
        context.application,
        pending["chat_id"],
        pending["url"],
        audio_only=audio_only,
        user_id=user_id,
    )


async def handle_hentai_playlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    pending = pending_hentai_playlist_requests.pop(user_id, None)

    if query.data == "hentai_playlist_cancel":
        await query.edit_message_text(f"{ICON_WARN} Playlist download cancelled.")
        return

    if not pending:
        await query.edit_message_text(f"{ICON_WARN} No pending playlist request found.")
        return

    playlist = pending["playlist"]
    await query.edit_message_text(build_hentai_playlist_started_text({
        "id": "new",
        "platform": playlist.site,
        "name": playlist.title,
        "episode_count": len(playlist.urls),
    }))
    await start_hentai_playlist_download(
        context.application,
        pending["chat_id"],
        playlist,
        user_id=user_id,
    )


async def start_download_from_source(
    app: Application,
    chat_id: int,
    source: str,
    user_id: int = None,
):
    """Mini-app download entrypoint that chooses the right backend for pasted links."""
    source = source.strip()
    http_url = extract_http_url(source)
    if is_hentai_playlist_url(http_url):
        playlist = await resolve_hentai_playlist(http_url)
        if not playlist.urls:
            raise RuntimeError("No episode links found on this playlist page.")
        return await start_hentai_playlist_download(app, chat_id, playlist, user_id=user_id)
    if is_video_url(http_url):
        return await start_ytdlp_download(
            app,
            chat_id,
            http_url,
            audio_only=False,
            user_id=user_id,
            run_in_background=True,
        )
    return await start_aria2_download(app, chat_id, source, user_id)


# =========================================================
# Pyrogram
# =========================================================

async def send_with_flood_wait_handling(send_coroutine, max_retries: int = 5):
    """
    Wraps a pyrogram send operation with FloodWait handling.
    Automatically waits and retries when Telegram rate-limits.
    
    Args:
        send_coroutine: The async send operation (e.g., client.send_document)
        max_retries: Maximum number of retries before giving up
        
    Returns:
        The result of the send operation
        
    Raises:
        FloodWait: If max_retries is exceeded
    """
    for attempt in range(max_retries):
        try:
            return await send_coroutine()
        except FloodWait as e:
            wait_time = e.value
            logger.warning(
                f"FloodWait: Telegram rate-limiting. "
                f"Waiting {wait_time} seconds (attempt {attempt + 1}/{max_retries})"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"FloodWait: Max retries exceeded after {max_retries} attempts")
                raise
        except Exception as e:
            logger.error(f"Error in send operation: {e}")
            raise


async def get_pyrogram_client():
    global pyro_client

    if not API_ID or not API_HASH:
        raise RuntimeError("API_ID or API_HASH not configured")

    if pyro_client is None:
        pyro_client = Client(
            PYRO_SESSION_NAME,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=str(BASE_DIR),
        )
        await pyro_client.start()

        me = await pyro_client.get_me()
        if getattr(me, "is_bot", False):
            await pyro_client.stop()
            pyro_client = None
            raise RuntimeError(
                f"Pyrogram session is logged in as a bot.\n"
                f'Delete "{PYRO_SESSION_NAME}.session" and log in with your personal Telegram account.'
            )

    return pyro_client


async def stop_pyrogram_client():
    global pyro_client
    if pyro_client is not None:
        try:
            await pyro_client.stop()
        except Exception:
            pass
        pyro_client = None


async def pyrogram_send_file(rel_path: str, progress_callback=None):
    client = await get_pyrogram_client()
    full = safe_join(DOWNLOAD_DIR, rel_path)

    if not full.exists():
        raise FileNotFoundError(str(full))
    if full.is_dir():
        raise IsADirectoryError(str(full))

    size = full.stat().st_size
    if size > MAX_SEND_SIZE:
        raise ValueError(f"File exceeds configured limit: {human_size(size)}")

    caption = build_upload_caption(rel_path)
    send_method = get_send_method(str(full))
    thumbnail_path = None

    try:
        # Generate thumbnail only when valid
        if send_method in ("video", "photo"):
            cleanup_orphan_thumbnails(full.parent)
            thumb = generate_thumbnail(str(full))

            # FIX: avoid passing empty string to Pyrogram
            if thumb and os.path.exists(thumb):
                thumbnail_path = thumb
            else:
                thumbnail_path = None

        if send_method == "video":
            video_meta = get_video_metadata(str(full))

            await send_with_flood_wait_handling(
                lambda: client.send_video(
                    chat_id="me",
                    video=str(full),
                    caption=caption,
                    thumb=thumbnail_path,
                    width=video_meta.get("width"),
                    height=video_meta.get("height"),
                    duration=video_meta.get("duration"),
                    supports_streaming=True,
                    progress=progress_callback,
                )
            )

        elif send_method == "audio":
            await send_with_flood_wait_handling(
                lambda: client.send_audio(
                    chat_id="me",
                    audio=str(full),
                    caption=caption,
                    progress=progress_callback,
                )
            )

        elif send_method == "photo":
            await send_with_flood_wait_handling(
                lambda: client.send_photo(
                    chat_id="me",
                    photo=str(full),
                    caption=caption,
                    progress=progress_callback,
                )
            )

        else:
            await send_with_flood_wait_handling(
                lambda: client.send_document(
                    chat_id="me",
                    document=str(full),
                    caption=caption,
                    thumb=thumbnail_path,
                    progress=progress_callback,
                )
            )

    finally:
        # Clean up thumbnail if created
        if thumbnail_path and os.path.exists(thumbnail_path):
            try:
                os.remove(thumbnail_path)
            except Exception:
                pass


async def update_upload_progress(app: Application, chat_id: int, message_id: int, upload_id: str, file_label: str, sent: int, total: int):
    now = time.time()
    
    if upload_id not in upload_jobs:
        return
    
    job = upload_jobs[upload_id]
    last_update = job.get("last_update", 0)

    if now - last_update < 1.5 and sent < total:
        return

    job["last_update"] = now
    pct = 0 if total == 0 else (sent / total) * 100
    progress_bar = build_progress_bar(sent, total, width=15)

    text = (
        f"{ICON_UPLOAD} Uploading\n\n"
        f"{file_label}\n"
        f"{progress_bar}\n\n"
        f"Target: your own Telegram account\n"
        f"{ICON_BOX} {human_size(sent)} / {human_size(total)}"
    )

    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")]
            ]),
        )
    except Exception:
        pass


async def send_single_file_via_pyrogram(
    app: Application,
    chat_id: int,
    message_id: int,
    rel_path: str,
    upload_id: str = None,
    user_id: int = None,
):
    if upload_id is None:
        async with upload_lock:
            global upload_counter
            upload_counter += 1
            upload_id = f"upload_{upload_counter}"
        upload_jobs[upload_id] = {
            "status": "uploading",
            "files": [rel_path],
            "current_file": 0,
            "chat_id": chat_id,
            "message_id": message_id,
            "last_update": 0,
        }
    
    job = upload_jobs[upload_id]
    full = safe_join(DOWNLOAD_DIR, rel_path)
    
    # Check if file needs to be split
    chunks = split_file_into_chunks(str(full))
    
    async def progress(current, total, chunk_idx=1, chunk_total=1):
        label = os.path.basename(rel_path)
        if chunk_total > 1:
            label = f"{label} (Part {chunk_idx}/{chunk_total})"
        await update_upload_progress(app, chat_id, message_id, upload_id, label, current, total)

    try:
        if len(chunks) > 1:
            # File was split, upload each chunk
            for chunk_path, chunk_num, total_chunks in chunks:
                async def progress_chunk(current, total):
                    await progress(current, total, chunk_num, total_chunks)
                
                try:
                    await pyrogram_send_file(chunk_path, progress_callback=progress_chunk)
                except RPCError as e:
                    msg = str(e)
                    if "USER_IS_BOT" in msg or "A bot cannot send messages to other bots or to itself" in msg:
                        raise RuntimeError(
                            f"Pyrogram is logged in as a bot.\n"
                            f'Delete "{PYRO_SESSION_NAME}.session" and restart the script.\n'
                            "Then log in with your personal Telegram account."
                        )
                    raise
        else:
            # Normal file upload
            await pyrogram_send_file(rel_path, progress_callback=progress)
            
    except RPCError as e:
        msg = str(e)
        if "USER_IS_BOT" in msg or "A bot cannot send messages to other bots or to itself" in msg:
            raise RuntimeError(
                f"Pyrogram is logged in as a bot.\n"
                f'Delete "{PYRO_SESSION_NAME}.session" and restart the script.\n'
                "Then log in with your personal Telegram account."
            )
        raise

    job["status"] = "completed"

    await maybe_delete_file_after_upload(user_id, rel_path)

    await app.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=(
            f"{ICON_OK} Upload Complete\n\n"
            f"Name: {os.path.basename(rel_path)}\n"
            f"Target: your own Telegram account\n"
            f"Parts sent: {len(chunks)}" if len(chunks) > 1 else f"{ICON_OK} Upload Complete\n\nName: {os.path.basename(rel_path)}\nTarget: your own Telegram account"
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")],
        ]),
    )


async def send_folder_files_via_pyrogram(
    app: Application,
    chat_id: int,
    message_id: int,
    rel_path: str,
    file_list: list = None,
    user_id: int = None,
):
    """Upload files from folder. If file_list is provided, upload only those files."""
    if file_list is None:
        files = get_all_files_in_folder(rel_path)
    else:
        files = file_list
    
    if not files:
        raise RuntimeError("No files to upload.")

    async with upload_lock:
        global upload_counter
        upload_counter += 1
        upload_id = f"upload_{upload_counter}"
    
    upload_jobs[upload_id] = {
        "status": "uploading",
        "files": files,
        "current_file": 0,
        "chat_id": chat_id,
        "message_id": message_id,
        "sent_count": 0,
        "last_update": 0,
    }

    sent_count = 0
    skipped = []
    total_files = len(files)

    for idx, file_rel in enumerate(files, start=1):
        full = safe_join(DOWNLOAD_DIR, file_rel)
        size = full.stat().st_size

        if size > MAX_SEND_SIZE:
            skipped.append(f"{file_rel} ({human_size(size)})")
            continue

        upload_jobs[upload_id]["current_file"] = idx

        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=(
                    f"{ICON_UPLOAD} Uploading Folder Files\n\n"
                    f"Folder: /{rel_path.lstrip('/')}\n"
                    f"File {idx}/{total_files}\n"
                    f"Now: {os.path.basename(file_rel)}\n"
                    f"Target: your own Telegram account"
                ),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")]
                ]),
            )
        except Exception:
            pass

        async def progress(current, total, file_label=os.path.basename(file_rel), i=idx):
            await update_upload_progress(
                app,
                chat_id,
                message_id,
                upload_id,
                f"File {i}/{total_files}: {file_label}",
                current,
                total
            )

        try:
            chunks = split_file_into_chunks(str(full))
            for chunk_path, chunk_num, total_chunks in chunks:
                async def progress_chunk(current, total, chunk_n=chunk_num, total_c=total_chunks, file_l=os.path.basename(file_rel), i_val=idx):
                    label = f"File {i_val}/{total_files}: {file_l}"
                    if total_c > 1:
                        label += f" (Part {chunk_n}/{total_c})"
                    await update_upload_progress(app, chat_id, message_id, upload_id, label, current, total)
                
                await pyrogram_send_file(chunk_path, progress_callback=progress_chunk)

                try:
                    if "_part_" in chunk_path and Path(chunk_path).exists():
                        Path(chunk_path).unlink()
                except Exception:
                    pass

            sent_count += 1
            upload_jobs[upload_id]["sent_count"] = sent_count
            await maybe_delete_file_after_upload(user_id, file_rel)
        except RPCError as e:
            msg = str(e)
            if "USER_IS_BOT" in msg or "A bot cannot send messages to other bots or to itself" in msg:
                raise RuntimeError(
                    f"Pyrogram is logged in as a bot.\n"
                    f'Delete "{PYRO_SESSION_NAME}.session" and restart the script.\n'
                    "Then log in with your personal Telegram account."
                )
            raise

    upload_jobs[upload_id]["status"] = "completed"

    text = (
        f"{ICON_OK} Folder Upload Complete\n\n"
        f"Folder: /{rel_path.lstrip('/')}\n"
        f"Target: your own Telegram account\n"
        f"Uploaded: {sent_count}/{total_files}"
    )

    if skipped:
        preview = "\n".join(skipped[:10])
        if len(skipped) > 10:
            preview += f"\n... and {len(skipped) - 10} more skipped"
        text += f"\n\n{ICON_WARN} Skipped oversized files:\n{preview}"

    await app.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")],
        ]),
    )


async def upload_mini_app_selection(
    app: Application,
    chat_id: int,
    files: list[str],
    user_id: int = None,
):
    if not files:
        raise RuntimeError("No files selected.")

    status_msg = await app.bot.send_message(
        chat_id=chat_id,
        text=f"{ICON_UPLOAD} Preparing mini-app upload ({len(files)} files)...",
    )
    await send_folder_files_via_pyrogram(
        app,
        chat_id,
        status_msg.message_id,
        "",
        file_list=files,
        user_id=user_id,
    )
    return f"Queued upload for {len(files)} file(s)."


async def zip_upload_mini_app_selection(
    app: Application,
    chat_id: int,
    files: list[str],
    user_id: int,
    job_id: str,
):
    job = mini_app_zip_jobs[job_id]
    try:
        settings = get_user_settings(user_id)
        source_paths = [safe_join(DOWNLOAD_DIR, rel_path) for rel_path in files]
        source_paths = filter_files_for_archiving(source_paths)
        if not source_paths:
            raise RuntimeError("No files selected.")

        files_to_zip = build_files_to_zip(source_paths)
        zip_name = f"miniapp_{int(time.time())}"
        job.update(
            {
                "status": "zipping",
                "phase": "zipping",
                "progress_text": f"Preparing {len(files_to_zip)} file(s)...",
                "file_count": len(files_to_zip),
                "created": [],
            }
        )

        async def on_progress(text: str):
            job["progress_text"] = clean_emoji_prefix(text)
            job["updated_at"] = now_ts()

        zip_paths, size_warnings = await run_archive_job(
            user_id,
            files_to_zip,
            DOWNLOAD_DIR,
            zip_name=zip_name,
            settings=settings,
            on_progress=on_progress,
        )

        job["phase"] = "uploading"
        job["status"] = "uploading"
        job["progress_text"] = f"Uploading {len(zip_paths)} archive volume(s)..."
        job["created"] = [p.name for p in zip_paths]
        job["updated_at"] = now_ts()

        all_ok = await send_archives_to_chat(app, chat_id, zip_paths, settings, None, user_id)

        if settings.get("auto_delete_files_after_zip"):
            for path in source_paths:
                try:
                    if path.is_file():
                        path.unlink()
                except Exception as exc:
                    logger.warning("Could not delete zipped source file %s: %s", path, exc)

        job["status"] = "completed" if all_ok else "failed"
        job["phase"] = "completed" if all_ok else "failed"
        job["progress_text"] = "ZIP created and uploaded." if all_ok else "Some archive volumes failed to upload."
        if size_warnings:
            job["warnings"] = size_warnings
        job["finished_at"] = now_ts()
    except Exception as exc:
        job["status"] = "failed"
        job["phase"] = "failed"
        job["progress_text"] = str(exc)
        job["finished_at"] = now_ts()
        logger.error("Mini-app zip job failed: %s", exc)


# =========================================================
# Aria2 RPC daemon manager
# =========================================================

ARIA2_DONE_STATES = {"complete", "error", "removed"}
JOB_ACTIVE_STATES = {"starting", "downloading", "metadata", "allocating", "queued", "paused", "processing"}


def _parse_int_field(value, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def extract_info_hash(text: str) -> str:
    """Return a normalized torrent info hash from a magnet or aria2 message."""
    decoded = unquote_plus(text or "")
    match = re.search(r"(?:btih:|InfoHash\s+)([A-Za-z0-9]+)", decoded, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _same_info_hash(left: str | None, right: str | None) -> bool:
    return bool(left and right and str(left).lower() == str(right).lower())


async def find_aria2_status_by_info_hash(info_hash: str) -> dict | None:
    if not info_hash:
        return None

    batches = [
        await aria2_client.tell_active(),
        await aria2_client.tell_waiting(0, 100),
        await aria2_client.tell_stopped(0, 100),
    ]
    for status in [item for batch in batches for item in batch]:
        if _same_info_hash(status.get("infoHash"), info_hash):
            return status
    return None


def _format_eta(total_length: int, completed_length: int, download_speed: int) -> str:
    if not total_length or not download_speed or completed_length >= total_length:
        return "Unknown"
    seconds = max(0, math.ceil((total_length - completed_length) / download_speed))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _extract_rpc_name(status: dict, fallback: str) -> str:
    bt_name = (status.get("bittorrent") or {}).get("info", {}).get("name")
    if bt_name:
        return clean_download_name(bt_name)
    for file_info in status.get("files") or []:
        path = file_info.get("path")
        if path:
            return clean_download_name(Path(path).name)
    return clean_download_name(fallback)


def _apply_aria2_status(job: dict, status: dict):
    total_length = _parse_int_field(status.get("totalLength"))
    completed_length = _parse_int_field(status.get("completedLength"))
    download_speed = _parse_int_field(status.get("downloadSpeed"))
    upload_length = _parse_int_field(status.get("uploadLength"))
    upload_speed = _parse_int_field(status.get("uploadSpeed"))
    connections = _parse_int_field(status.get("connections"))
    num_seeders = _parse_int_field(status.get("numSeeders"))
    rpc_status = status.get("status", "unknown")

    job["aria2_status"] = rpc_status
    job["aria2_gid"] = status.get("gid", job.get("gid"))
    job["followed_by"] = status.get("followedBy") or []
    job["following"] = status.get("following")
    job["info_hash"] = status.get("infoHash") or job.get("info_hash")
    job["connections"] = connections
    job["num_seeders"] = num_seeders
    job["total_length"] = total_length
    job["completed_length"] = completed_length
    job["download_speed"] = download_speed
    job["upload_length"] = upload_length
    job["upload_speed"] = upload_speed
    job["progress"] = (completed_length / total_length * 100) if total_length else 0.0
    job["eta"] = _format_eta(total_length, completed_length, download_speed)
    job["name"] = _extract_rpc_name(status, job["name"])

    if rpc_status == "active":
        job["status"] = "metadata" if not total_length and job.get("source_type") == "magnet" else "downloading"
    elif rpc_status == "waiting":
        job["status"] = "queued"
    elif rpc_status == "paused":
        job["status"] = "paused"
    elif rpc_status == "complete":
        if status.get("followedBy"):
            job["status"] = "metadata"
        else:
            job["status"] = "completed"
            job["progress"] = 100.0
    elif rpc_status == "error":
        job["status"] = "failed"
        message = status.get("errorMessage") or status.get("errorCode") or "Unknown error"
        job["last_line"] = str(message)
    elif rpc_status == "removed":
        job["status"] = "cancelled"


def _split_torrent_source(source: str) -> tuple[str, str | None]:
    if " --select-file=" in source:
        torrent_path, selected = source.split(" --select-file=", 1)
        return torrent_path.strip(), selected.strip()
    return source.strip(), None


def _is_local_torrent_file(source: str) -> bool:
    lowered = source.lower()
    if lowered.startswith(("magnet:", "http://", "https://")):
        return False

    try:
        source_path = Path(source)
        return source_path.suffix.lower() == ".torrent" and source_path.exists()
    except OSError:
        return False


def build_download_started_text(job: dict) -> str:
    source_type = job.get("source_type", "torrent")
    title = shorten(clean_download_name(job.get("name", "Download")), 90)
    action = "Download reattached" if job.get("last_line") == "Reattached to existing aria2 download." else "Download started"
    mode_label = {
        "http": "direct HTTP/HTTPS",
        "magnet": "magnet",
        "torrent": "torrent",
        "uri": "direct URI",
    }.get(source_type, source_type)
    icon = ICON_DOWNLOAD if source_type in ("http", "uri") else ICON_MAGNET
    return (
        f"{icon} {action}\n\n"
        f"Job: #{job['id']}\n"
        f"Title:\n{title}\n\n"
        f"Engine: aria2 daemon\n"
        f"GID: {job.get('gid', 'unknown')}\n"
        f"Mode: {mode_label}\n"
        f"Folder:\n{DOWNLOAD_DIR}"
    )


def build_download_completed_text(job: dict) -> str:
    title = shorten(clean_download_name(job.get("name", "Download")), 90)
    total = int(job.get("total_length", 0) or job.get("completed_length", 0) or 0)
    uploaded = int(job.get("upload_length", 0) or 0)
    lines = [
        f"{ICON_OK} Download completed",
        "",
        f"Job: #{job['id']}",
        f"Title:\n{title}",
    ]
    if total:
        lines.append(f"Size: {human_size(total)}")
    if uploaded:
        lines.append(f"Uploaded while active: {human_size(uploaded)}")
    if job.get("metadata_gid"):
        lines.append(f"Metadata GID: {job['metadata_gid']}")
    lines.append(f"Download GID: {job.get('gid', 'unknown')}")
    lines.append(f"Folder:\n{DOWNLOAD_DIR}")
    return "\n".join(lines)


def switch_to_followed_gid(job: dict, status: dict) -> bool:
    followed_by = status.get("followedBy") or []
    if not followed_by:
        return False

    next_gid = followed_by[0]
    if not next_gid or next_gid == job.get("gid"):
        return False

    previous_gid = job.get("gid")
    job["metadata_gid"] = previous_gid
    job.setdefault("gid_history", []).append(previous_gid)
    job["gid"] = next_gid
    job["aria2_gid"] = next_gid
    job["status"] = "queued"
    job["aria2_status"] = "waiting"
    job["last_line"] = "Torrent metadata resolved; following real download."
    return True


async def monitor_aria2_job(app: Application, job_id: int):
    job = download_jobs[job_id]

    while job["status"] not in ("completed", "failed", "cancelled"):
        try:
            status = await aria2_client.tell_status(job["gid"])
            _apply_aria2_status(job, status)
            if switch_to_followed_gid(job, status):
                await maybe_auto_update_status_message(app, job, force=True)
                await asyncio.sleep(1)
                continue
            await maybe_auto_update_status_message(app, job)
        except Aria2RpcError as exc:
            if "not found" in str(exc).lower() and job["status"] == "cancelled":
                return
            job["status"] = "failed"
            job["last_line"] = str(exc)
            break

        if status.get("status") in ARIA2_DONE_STATES:
            break

        await asyncio.sleep(2)

    if job["status"] == "cancelled":
        job["finished_at"] = now_ts()
        await maybe_auto_update_status_message(app, job, force=True)
        return

    if job["status"] == "completed":
        job["status"] = "completed"
        logger.info(f"Download completed: {job['name']}")
        job["progress"] = 100.0
        job["finished_at"] = now_ts()
        await maybe_auto_update_status_message(app, job, force=True)

        try:
            await app.bot.send_message(
                chat_id=job["chat_id"],
                text=build_download_completed_text(job),
                reply_markup=build_reply_menu(),
            )
        except Exception:
            pass

    else:
        job["status"] = "failed"
        logger.error(f"Download failed: {job['name']}")
        job["finished_at"] = now_ts()
        await maybe_auto_update_status_message(app, job, force=True)

        try:
            await app.bot.send_message(
                chat_id=job["chat_id"],
                text=(
                    f"{ICON_FAIL} Download failed.\n\n"
                    f"Job: #{job['id']}\n"
                    f"Title:\n{shorten(clean_download_name(job['name']), 90)}\n\n"
                    f"Reason: {job.get('last_line', 'Unknown error')}"
                ),
                reply_markup=build_reply_menu(),
            )
        except Exception:
            pass


async def start_aria2_download(app: Application, chat_id: int, magnet: str, user_id: int = None):
    global job_counter, download_jobs

    source, selected_files = _split_torrent_source(magnet)
    is_torrent_file = _is_local_torrent_file(source)
    lowered_source = source.lower()
    is_http_uri = lowered_source.startswith(("http://", "https://"))
    source_type = "torrent" if is_torrent_file else "magnet" if lowered_source.startswith("magnet:") else "http" if is_http_uri else "uri"
    if source_type == "magnet":
        name = extract_bt_name(source)
    elif source_type == "http":
        name = extract_http_filename(source)
    elif is_torrent_file:
        name = clean_download_name(Path(source).name)
    else:
        name = "Download"
    info_hash = extract_info_hash(source)
    options = {
        "dir": str(DOWNLOAD_DIR),
        "continue": "true",
        "follow-torrent": "true",
        "bt-save-metadata": "true",
        "bt-metadata-only": "false",
        "seed-time": "0",
    }
    if selected_files:
        options["select-file"] = selected_files

    initial_status = None
    reattached = False
    try:
        if is_torrent_file:
            gid = await aria2_client.add_torrent(Path(source), options)
        else:
            gid = await aria2_client.add_uri(source, options)
    except Aria2RpcError as exc:
        duplicate_hash = extract_info_hash(str(exc)) or info_hash
        if "already registered" not in str(exc).lower() or not duplicate_hash:
            raise

        initial_status = await find_aria2_status_by_info_hash(duplicate_hash)
        if not initial_status:
            raise

        gid = initial_status["gid"]
        info_hash = duplicate_hash
        reattached = True

    async with jobs_lock:
        job_counter += 1
        job_id = job_counter

    job = {
        "id": job_id,
        "name": name,
        "magnet": source,
        "gid": gid,
        "gid_history": [gid],
        "metadata_gid": None,
        "source_type": source_type,
        "chat_id": chat_id,
        "user_id": user_id or 0,
        "pid": aria2_client.pid,
        "process": None,
        "status": "starting",
        "aria2_status": "starting",
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "upload_length": 0,
        "upload_speed": 0,
        "connections": 0,
        "num_seeders": 0,
        "info_hash": info_hash,
        "followed_by": [],
        "following": None,
        "eta": "Unknown",
        "started_at": now_ts(),
        "finished_at": None,
        "last_line": "Reattached to existing aria2 download." if reattached else "",
    }
    if initial_status:
        _apply_aria2_status(job, initial_status)
        switch_to_followed_gid(job, initial_status)

    download_jobs[job_id] = job

    asyncio.create_task(monitor_aria2_job(app, job_id))

    return job


async def cancel_job(job_id: int):
    job = download_jobs.get(job_id)
    if not job:
        return False, f"Job #{job_id} not found."

    if job["status"] in ("completed", "failed", "cancelled"):
        return False, f"Job #{job_id} is already {job['status']}."

    process = job.get("process")
    if process is not None and job.get("provider") == "spotify":
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            pass
        except Exception as exc:
            return False, f"Could not cancel job #{job_id}: {exc}"

        job["status"] = "cancelled"
        job["finished_at"] = now_ts()
        job["last_line"] = "Cancelled by user"
        return True, f"Cancelled job #{job_id}: {job['name']}"

    if job.get("provider") in {"manga", "yt-dlp", "hentai-playlist"}:
        job["status"] = "cancelled"
        job["finished_at"] = now_ts()
        job["last_line"] = "Cancelled by user"
        return True, f"Cancelled job #{job_id}: {job['name']}"

    errors = []
    for gid in dict.fromkeys([job.get("gid"), job.get("metadata_gid")]):
        if not gid:
            continue
        try:
            await aria2_client.remove(gid, force=True)
        except Aria2RpcError as exc:
            if "not found" not in str(exc).lower():
                errors.append(str(exc))
    if errors:
        return False, f"Could not cancel job #{job_id}: {'; '.join(errors)}"

    job["status"] = "cancelled"
    job["aria2_status"] = "removed"
    job["finished_at"] = now_ts()
    return True, f"Cancelled job #{job_id}: {job['name']}"


async def pause_job(job_id: int):
    job = download_jobs.get(job_id)
    if not job:
        return False, f"Job #{job_id} not found."
    if job["status"] not in JOB_ACTIVE_STATES or job["status"] == "paused":
        return False, f"Job #{job_id} cannot be paused from {job['status']}."

    try:
        await aria2_client.pause(job["gid"], force=True)
    except Aria2RpcError as exc:
        return False, f"Could not pause job #{job_id}: {exc}"

    job["status"] = "paused"
    job["aria2_status"] = "paused"
    return True, f"Paused job #{job_id}."


async def resume_job(job_id: int):
    job = download_jobs.get(job_id)
    if not job:
        return False, f"Job #{job_id} not found."
    if job["status"] != "paused":
        return False, f"Job #{job_id} is not paused."

    try:
        await aria2_client.unpause(job["gid"])
    except Aria2RpcError as exc:
        return False, f"Could not resume job #{job_id}: {exc}"

    job["status"] = "queued"
    job["aria2_status"] = "waiting"
    return True, f"Resumed job #{job_id}."


def clear_finished_jobs():
    global download_jobs

    keep = {}
    for jid, job in download_jobs.items():
        if job["status"] in JOB_ACTIVE_STATES:
            keep[jid] = job

    removed = len(download_jobs) - len(keep)
    download_jobs = keep
    return removed


async def refresh_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await status_cmd(update, context)


# =========================================================
# Zipping Feature - Handlers
# =========================================================

async def zip_files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create zip archive of files in Download folder."""
    user_id = update.effective_user.id

    if not is_authorized_user(user_id):
        await update.message.reply_text("⛔ Unauthorized")
        return

    try:
        files_only = filter_files_for_archiving(collect_download_files())

        if not files_only:
            await update.message.reply_text("📭 No files to zip in Download folder")
            return

        parts = (update.message.text or "").split(maxsplit=1)
        zip_name = parts[1].strip() if len(parts) > 1 else f"archive_{int(time.time())}"

        files_only, limit_warn = apply_zip_file_limit(files_only)
        settings = get_user_settings(user_id)
        files_to_zip = build_files_to_zip(files_only)
        
        # Get compression method for display
        method = settings.get("zip_method", "zip").upper()
        compression_info = f"Method: {method} | Level: {settings.get('compression_level', 5)}/9"

        status_msg = await update.message.reply_text(
            f"📦 Preparing to zip {len(files_to_zip)} file(s)...\n"
            f"Name: {zip_sanitize(zip_name)}\n"
            f"{compression_info}\n"
            f"Please wait..."
        )

        async def on_progress(text: str):
            try:
                await status_msg.edit_text(text)
            except BadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise

        # Create upload callback for instant upload + delete
        upload_callback = create_zip_upload_callback(
            context, update.effective_chat.id, user_id, settings, status_msg
        )

        zip_paths, size_warnings = await run_archive_job(
            user_id,
            files_to_zip,
            DOWNLOAD_DIR,
            zip_name=zip_name,
            settings=settings,
            on_progress=on_progress,
            upload_callback=upload_callback,
        )

        # If all parts were uploaded and deleted by callback, zip_paths will be empty
        if zip_paths:
            all_ok = await send_archives_to_chat(
                context, update.effective_chat.id, zip_paths, settings, status_msg, user_id
            )
        else:
            all_ok = True  # All parts were already sent via callback

        done_text = f"✅ Zip complete! Uploaded volume(s)"
        if limit_warn:
            done_text += f"\n{limit_warn}"
        for w in size_warnings:
            done_text += f"\n⚠️ {w}"
        if not all_ok:
            done_text += "\n⚠️ Some archives failed to send."
        done_text += "\n📊 All zip parts have been automatically deleted from disk to save space."
        await status_msg.edit_text(done_text)

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def list_files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all files in Download folder."""
    user_id = update.effective_user.id
    
    if not is_authorized_user(user_id):
        await update.message.reply_text("⛔ Unauthorized")
        return
    
    try:
        files_only = collect_download_files()

        if not files_only:
            await update.message.reply_text("📭 Download folder is empty")
            return

        text_lines = [f"📁 Files ({len(files_only)} total):"]
        total_size = 0
        
        for i, f in enumerate(files_only[:50], 1):  # Show first 50
            rel_path = f.relative_to(DOWNLOAD_DIR)
            size = f.stat().st_size
            total_size += size
            text_lines.append(f"{i}. {rel_path.name} ({zip_human_size(size)})")
        
        if len(files_only) > 50:
            text_lines.append(f"\n... and {len(files_only) - 50} more files")
        
        text_lines.append(f"\n📊 Total size: {zip_human_size(total_size)}")
        
        await update.message.reply_text("\n".join(text_lines))
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def clear_files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all files from Download folder."""
    user_id = update.effective_user.id
    
    if not is_authorized_user(user_id):
        await update.message.reply_text("⛔ Unauthorized")
        return
    
    try:
        all_items = list(DOWNLOAD_DIR.glob("*"))
        
        if not all_items:
            await update.message.reply_text("📭 Download folder already empty")
            return
        
        # Delete all files (not folders to be safe)
        deleted_count = 0
        total_freed = 0
        
        for item in all_items:
            try:
                if item.is_file():
                    size = item.stat().st_size
                    item.unlink()
                    deleted_count += 1
                    total_freed += size
                elif item.is_dir() and not any(item.iterdir()):
                    item.rmdir()
            except Exception as e:
                logger.warning(f"Could not delete {item}: {e}")
        
        await update.message.reply_text(
            f"🗑 Cleared {deleted_count} file(s)\n"
            f"Freed: {zip_human_size(total_freed)}"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


# =========================================================
# Commands
# =========================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_authorized_user(user_id):
        await update.message.reply_text("Unauthorized")
        return

    await update.message.reply_text(
        build_home_text(user_id), 
        reply_markup=build_reply_menu(user_id)
    )
    
    # Show the file browser mini-app button
    if WEB_APP_ENABLE:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "Open Modern File Browser",
                    web_app={"url": WEB_APP_URL}
                )
            ]
        ])
        
        await update.message.reply_text(
            "Or use our modern file browser:",
            reply_markup=keyboard
        )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await update_status_message(context.application, chat_id, user_id)


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_authorized_user(user_id):
        await update.message.reply_text("⛔ Unauthorized")
        return

    await update.message.reply_text(
        build_files_text("", 0),
        reply_markup=build_files_markup("", 0),
        disable_web_page_preview=True,
    )


async def browse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open the modern file browser mini-app."""
    user_id = update.effective_user.id

    if not is_authorized_user(user_id):
        await update.message.reply_text("Unauthorized")
        return

    # Use inline keyboard with Web App button
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Open File Browser",
                web_app={"url": WEB_APP_URL}
            )
        ]
    ])
    
    await update.message.reply_text(
        "Modern File Browser\n\n"
        "Features:\n"
        "- Browse all downloaded files\n"
        "- Batch delete files\n"
        "- Create archives (ZIP/7Z)\n"
        "- Upload to Telegram\n"
        "- Search and filter\n"
        "- Real-time statistics\n\n"
        "Click the button below to open the file browser.",
        reply_markup=keyboard
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_authorized_user(user_id):
        await update.message.reply_text("â›” Unauthorized")
        return

    await update.message.reply_text(
        build_zip_settings_text(user_id),
        reply_markup=build_zip_settings_markup(user_id),
        disable_web_page_preview=True,
    )


async def manga_settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_authorized_user(user_id):
        await update.message.reply_text("Unauthorized")
        return

    await update.message.reply_text(
        build_manga_settings_text(user_id),
        reply_markup=build_manga_settings_markup(user_id),
        disable_web_page_preview=True,
    )


async def forwarded_posts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_authorized_user(user_id):
        await update.message.reply_text("â›” Unauthorized")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            f"{format_forwarded_posts_setting(user_id)}\n\n"
            "Usage: /forwardedposts on|off",
            reply_markup=build_reply_menu(user_id),
        )
        return

    value = args[0].lower()
    if value not in ("on", "off"):
        await update.message.reply_text("Usage: /forwardedposts on|off")
        return

    enabled = value == "on"
    await update_setting(user_id, "auto_download_forwarded_posts", enabled)
    await update.message.reply_text(
        f"Forwarded post auto-download is now {'ON' if enabled else 'OFF'}.",
        reply_markup=build_reply_menu(user_id),
    )


# =========================================================
# Zip Menu Functions
# =========================================================

def build_zip_menu_text(user_id: int) -> str:
    """Build text for zip menu."""
    files_count = len(filter_files_for_archiving(collect_download_files()))
    
    return (
        f"{ICON_ARCHIVE} Archive / Zip Menu\n\n"
        f"Available files: {files_count}\n\n"
        "Create archives, choose files, or adjust archive defaults."
    )


def build_zip_menu_markup(user_id: int) -> InlineKeyboardMarkup:
    """Build buttons for zip menu."""
    u = user_id or 0
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ICON_FILE} {clean_emoji_prefix(get_lang(u, 'list_files'))}", callback_data="zip_menu:list")],
        [InlineKeyboardButton(f"{ICON_OK} {clean_emoji_prefix(get_lang(u, 'select_files'))}", callback_data="zip_menu:select")],
        [InlineKeyboardButton(f"{ICON_ARCHIVE} {clean_emoji_prefix(get_lang(u, 'zip_all'))}", callback_data="zip_menu:zip_all")],
        [InlineKeyboardButton(f"{ICON_SETTINGS} {clean_emoji_prefix(get_lang(u, 'zip_settings'))}", callback_data="zip_menu:settings")],
        [InlineKeyboardButton(f"{ICON_HOME} Main Menu", callback_data="menu_home")],
    ])


def build_zip_file_list_markup(user_id: int, page: int = 0, files_per_page: int = 10) -> tuple:
    """Build text and markup for zip file list."""
    u = user_id or 0
    
    all_files = filter_files_for_archiving(collect_download_files())

    if not all_files:
        return (
            f"{clean_emoji_prefix(get_lang(u, 'list_files'))}\n\n{get_lang(u, 'no_files')}",
            InlineKeyboardMarkup([[InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'home_btn'))}", callback_data="zip_menu:back")]])
        )
    
    total_pages = (len(all_files) + files_per_page - 1) // files_per_page
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * files_per_page
    end_idx = start_idx + files_per_page
    page_files = all_files[start_idx:end_idx]
    
    lines = [
        f"{clean_emoji_prefix(get_lang(u, 'list_files'))}",
        f"Page {page + 1}/{total_pages}",
        ""
    ]
    
    total_size = 0
    for i, f in enumerate(page_files, start_idx + 1):
        try:
            size = f.stat().st_size
            total_size += size
            rel_path = f.relative_to(DOWNLOAD_DIR)
            lines.append(f"{i}. {rel_path.name} ({zip_human_size(size)})")
        except Exception:
            pass

    lines.append(f"\n📊 Total on page: {zip_human_size(total_size)}")
    lines.append(f"📦 Total files: {len(all_files)}")

    buttons = []
    if page > 0:
        buttons.append(InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'prev'))}", callback_data=f"zip_menu:list:{page-1}"))
    if page < total_pages - 1:
        buttons.append(InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'next'))}", callback_data=f"zip_menu:list:{page+1}"))
    
    keyboard = []
    if buttons:
        keyboard.append(buttons)
    keyboard.append([InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'home_btn'))}", callback_data="zip_menu:back")])
    
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def build_zip_file_select_markup(user_id: int, page: int = 0, files_per_page: int = 10) -> tuple:
    """Build text and markup for zip file selection."""
    u = user_id or 0
    
    all_files = filter_files_for_archiving(collect_download_files())

    if not all_files:
        return (
            f"{clean_emoji_prefix(get_lang(u, 'select_files'))}\n\n{get_lang(u, 'no_files')}",
            InlineKeyboardMarkup([[InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'home_btn'))}", callback_data="zip_menu:back")]])
        )
    
    session = zip_select_sessions.get(user_id, {"selected": set(), "page": page})
    zip_select_sessions[user_id] = session
    session["page"] = page
    
    total_pages = (len(all_files) + files_per_page - 1) // files_per_page
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * files_per_page
    end_idx = start_idx + files_per_page
    page_files = all_files[start_idx:end_idx]
    
    lines = [
        f"{clean_emoji_prefix(get_lang(u, 'select_files'))}",
        f"Page {page + 1}/{total_pages}",
        f"Selected: {len(session['selected'])}/{len(all_files)}",
        ""
    ]
    
    selected_size = 0
    for display_num, f in enumerate(page_files, start_idx + 1):
        try:
            size = f.stat().st_size
            rel_path = f.relative_to(DOWNLOAD_DIR)
            token = encode_path(file_rel_path(f))
            is_selected = token in session["selected"]
            checkbox = "✅" if is_selected else "☐"
            lines.append(f"{checkbox} {display_num}. {rel_path.name} ({zip_human_size(size)})")
            if is_selected:
                selected_size += size
        except Exception:
            pass

    for token in session["selected"]:
        try:
            full = safe_join(DOWNLOAD_DIR, decode_path(token))
            if full.is_file() and full not in page_files:
                selected_size += full.stat().st_size
        except Exception:
            pass

    lines.append(f"\n📊 Selected size: {zip_human_size(selected_size)}")

    keyboard = []
    for f in page_files:
        token = encode_path(file_rel_path(f))
        file_name = f.name[:25] + "..." if len(f.name) > 25 else f.name
        is_selected = token in session["selected"]
        checkbox = "✅" if is_selected else "☐"
        keyboard.append([
            InlineKeyboardButton(f"{checkbox} {file_name}", callback_data=f"zip_select:{token}")
        ])
    
    # Navigation and action buttons
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'prev'))}", callback_data=f"zip_menu:select:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'next'))}", callback_data=f"zip_menu:select:{page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton(f"💾 Save Selection", callback_data="zip_select:confirm")])
    keyboard.append([InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'home_btn'))}", callback_data="zip_menu:back")])
    
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def build_zip_settings_text(user_id: int) -> str:
    """Build text for zip settings."""
    u = user_id or 0
    return format_settings_text(user_id) + "\n\nTap buttons below to change settings:"


def build_zip_settings_markup(user_id: int) -> InlineKeyboardMarkup:
    """Build buttons for zip settings."""
    u = user_id or 0
    settings = get_user_settings(user_id)
    part_size_mb = settings.get("zip_part_size", 1 * 1024 * 1024 * 1024) // (1024 * 1024)
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📦 Part Size: {part_size_mb}MB", callback_data="zip_setting:part_size")],
        [InlineKeyboardButton(f"📋 Method: {settings.get('zip_method', 'zip').upper()}", callback_data="zip_setting:method")],
        [InlineKeyboardButton(f"🔐 Password: {'Set' if settings.get('password') else 'None'}", callback_data="zip_setting:password")],
        [InlineKeyboardButton(f"🗑 Auto-delete files: {'✅' if settings.get('auto_delete_files_after_zip') else '❌'}", callback_data="zip_setting:auto_del_files")],
        [InlineKeyboardButton(f"🗑 Auto-delete zips: {'✅' if settings.get('auto_delete_zips_after_send') else '❌'}", callback_data="zip_setting:auto_del_zips")],
        [InlineKeyboardButton(f"🗑 Auto-delete after upload: {'✅' if settings.get('auto_delete_files_after_upload') else '❌'}", callback_data="zip_setting:auto_del_upload")],
        [InlineKeyboardButton(f"📥 Forwarded posts: {'✅' if settings.get('auto_download_forwarded_posts') else '❌'}", callback_data="zip_setting:forwarded_posts")],
        [InlineKeyboardButton(f"🔨 Compression: {settings.get('compression_level', 5)}/9", callback_data="zip_setting:compression")],
        [InlineKeyboardButton(f"🏠 Back", callback_data="zip_menu:back")],
    ])


# =========================================================
# Text handler
# =========================================================

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    lower = text.lower()

    # Check if user is waiting for password input (PRIORITY: this must be checked BEFORE zip name)
    if user_id in zip_select_sessions and zip_select_sessions[user_id].get("waiting_for") == "password":
        if lower == "none":
            await update_setting(user_id, "password", "")
            await update.message.reply_text("✅ Password removed")
        else:
            if not validate_password(text):
                await update.message.reply_text("❌ Password too long (max 100 characters)")
                return
            settings = get_user_settings(user_id)
            warn = check_password_support(text, settings.get("zip_method", "zip"))
            await update_setting(user_id, "password", text)
            msg = "✅ Password set"
            if warn:
                msg += f"\n⚠️ {warn}"
            await update.message.reply_text(msg)

        session = zip_select_sessions.get(user_id, {})
        session.pop("waiting_for", None)
        zip_select_sessions[user_id] = session
        return

    # Check if user is waiting for zip name
    if user_id in pending_zip_name_sessions:
        session = pending_zip_name_sessions[user_id]
        mode = session.get("mode")
        
        if not text or len(text) > 100:
            await update.message.reply_text("❌ Please provide a valid zip name (1-100 characters)")
            return
        
        # Sanitize zip name
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', text).strip('._- ')
        if not safe_name:
            await update.message.reply_text("❌ Invalid zip name. Please try again.")
            return
        
        try:
            files_to_zip = session.get("files_to_zip", [])
            source_files = session.get("source_files", [])
            settings = session.get("settings", {})
            limit_warn = session.get("limit_warn", "")
            
            status_msg = await update.message.reply_text(f"📦 {get_lang(user_id, 'zipping')}...")
            
            async def on_progress(prog_text: str):
                try:
                    await status_msg.edit_text(prog_text)
                except BadRequest as e:
                    if "message is not modified" not in str(e).lower():
                        pass
            
            # Create upload callback for instant upload + delete
            upload_callback = create_zip_upload_callback(
                context, chat_id, user_id, settings, status_msg
            )

            zip_paths, size_warnings = await run_archive_job(
                user_id,
                files_to_zip,
                DOWNLOAD_DIR,
                zip_name=safe_name,
                settings=settings,
                on_progress=on_progress,
                upload_callback=upload_callback,
            )
            
            # If all parts were uploaded and deleted by callback, zip_paths will be empty
            if zip_paths:
                all_ok = await send_archives_to_chat(
                    context, chat_id, zip_paths, settings, status_msg, user_id
                )
            else:
                all_ok = True  # All parts were already sent via callback
            
            if all_ok and settings.get("auto_delete_files_after_zip"):
                for f in source_files:
                    try:
                        if isinstance(f, Path):
                            f.unlink()
                    except Exception:
                        pass
            
            # Clear pending session
            pending_zip_name_sessions.pop(user_id, None)
            
            done_text = (
                f"{clean_emoji_prefix(get_lang(user_id, 'zip_complete'))}\n"
                f"Uploaded volume(s)"
            )
            if limit_warn:
                done_text += f"\n{limit_warn}"
            for w in size_warnings:
                done_text += f"\n⚠️ {w}"
            if not all_ok:
                done_text += "\n⚠️ Some archives failed to send."
            done_text += "\n📊 All zip parts have been automatically deleted from disk to save space."
            
            await status_msg.edit_text(done_text)
        
        except Exception as e:
            logger.error(f"Zip error during execution: {e}")
            await update.message.reply_text(f"{clean_emoji_prefix(get_lang(user_id, 'zip_error'))}: {e}")
            pending_zip_name_sessions.pop(user_id, None)
        
        return

    # normalize button text by removing emojis and extra spaces
    normalized = re.sub(r'[^\w\s\u0600-\u06FF]', '', lower).strip()

    try:
        if normalized in ("home", "main menu", "منوی اصلی"):
            await update.message.reply_text(
                build_home_text(user_id), 
                reply_markup=build_reply_menu(user_id)
            )

        elif normalized in ("status", "?????"):
            await update_status_message(context.application, chat_id, user_id)

        elif normalized in ("downloads", "download"):
            await update.message.reply_text(
                build_downloads_menu_text(user_id),
                reply_markup=build_downloads_menu_markup(user_id),
                disable_web_page_preview=True,
            )

        elif normalized in ("tools", "tool"):
            await update.message.reply_text(
                build_tools_menu_text(user_id),
                reply_markup=build_tools_menu_markup(user_id),
                disable_web_page_preview=True,
            )

        elif normalized in ("settings", "setting"):
            await update.message.reply_text(
                build_settings_menu_text(user_id),
                reply_markup=build_settings_menu_markup(user_id),
                disable_web_page_preview=True,
            )

        elif normalized in ("queue", "صف"):
            await update.message.reply_text(
                build_queue_text(user_id), 
                reply_markup=build_reply_menu(user_id)
            )

        elif normalized in ("files",):
            await update.message.reply_text(
                build_files_menu_text(user_id),
                reply_markup=build_files_menu_markup(user_id),
                disable_web_page_preview=True,
            )

        elif normalized in ("file browser", "مرورگر فایل"):
            await update.message.reply_text(
                build_files_text("", 0),
                reply_markup=build_files_markup("", 0),
                disable_web_page_preview=True,
            )

        elif normalized in ("cancel", "انصراف"):
            active = [j for j in download_jobs.values() if j["status"] in JOB_ACTIVE_STATES]

            if not active:
                await update.message.reply_text(
                    f"{ICON_STOP} {clean_emoji_prefix(get_lang(user_id, 'no_active'))}",
                    reply_markup=build_reply_menu(user_id)
                )
            else:
                lines = [f"{ICON_STOP} {clean_emoji_prefix(get_lang(user_id, 'cancel_help'))}", ""]
                for j in sorted(active, key=lambda x: x["id"]):
                    lines.append(f"#{j['id']} [{j['status']}] {j['name']}")
                lines.append("")
                lines.append(get_lang(user_id, 'cancel_help2'))
                await update.message.reply_text("\n".join(lines), reply_markup=build_reply_menu(user_id))

        elif lower.startswith("cancel "):
            m = re.match(r"cancel\s+(\d+)", lower)
            if not m:
                await update.message.reply_text(
                    get_lang(user_id, 'usage'),
                    reply_markup=build_reply_menu(user_id)
                )
                return

            jid = int(m.group(1))
            if jid not in download_jobs:
                await update.message.reply_text(
                    f"Job #{jid} {get_lang(user_id, 'not_found')}",
                    reply_markup=build_reply_menu(user_id)
                )
                return
            
            job = download_jobs[jid]
            await update.message.reply_text(
                f"{ICON_WARN} {get_lang(user_id, 'confirm_cancel_job')}\n\n"
                f"Job #{jid}\n"
                f"{job['name']}\n\n"
                f"Status: {job['status']}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(f"{ICON_STOP} Yes, Cancel", callback_data=f"cancel_confirm:{jid}"),
                    ]
                ]),
            )

        elif lower in ("clear", f"{ICON_BROOM.lower()} clear", get_lang(user_id, 'clear').lower()):
            msg = await update.message.reply_text(
                f"{ICON_WARN} {get_lang(user_id, 'confirm_clear')}\n\n"
                f"{get_lang(user_id, 'clear_warning')}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(f"{ICON_BROOM} Yes, Clear", callback_data="clear_confirm"),
                    ]
                ]),
            )

        elif "help" in normalized or "راهنما" in normalized:
            await update.message.reply_text(
                build_help_text(user_id), 
                reply_markup=build_reply_menu(user_id)
            )

        elif normalized in ("settings", "zip settings"):
            await update.message.reply_text(
                build_zip_settings_text(user_id),
                reply_markup=build_zip_settings_markup(user_id),
                disable_web_page_preview=True,
            )

        elif normalized in ("manga settings", "manga"):
            await update.message.reply_text(
                build_manga_settings_text(user_id),
                reply_markup=build_manga_settings_markup(user_id),
                disable_web_page_preview=True,
            )

        elif get_lang(user_id, 'toggle_language').lower() in lower or "language" in lower or "زبان" in text:
            lang_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(get_lang_for_all("en", "en"), callback_data="set_lang:en"),
                    InlineKeyboardButton(get_lang_for_all("fa", "fa"), callback_data="set_lang:fa"),
                ]
            ])
            await update.message.reply_text(
                f"{get_lang(user_id, 'language')}\n\n{get_lang(user_id, 'select_language')}",
                reply_markup=lang_keyboard,
            )

        elif text.startswith("magnet:?") or text.startswith("magnet:"):
            name = extract_bt_name(text)
            if is_duplicate_name(name):
                await update.message.reply_text(
                    f"{ICON_WARN} {get_lang(user_id, 'duplicate_detected')} {name}",
                    reply_markup=build_reply_menu(user_id)
                )
                return

            job = await start_aria2_download(context.application, chat_id, text, user_id)

            await update.message.reply_text(
                build_download_started_text(job),
                reply_markup=build_reply_menu(user_id),
                disable_web_page_preview=True,
            )
            await update_status_message(context.application, chat_id, user_id)

        elif is_manga_url(text):
            manga_url = extract_manga_url(text)
            pending_manga_requests[user_id] = {
                "url": manga_url,
                "chat_id": chat_id,
            }
            await update.message.reply_text(
                build_manga_prompt_text(manga_url),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Continue", callback_data="manga_confirm"),
                        InlineKeyboardButton("Cancel", callback_data="manga_cancel"),
                    ],
                ]),
                disable_web_page_preview=True,
            )

        elif is_spotify_url(text):
            spotify_url = extract_spotify_url(text)
            pending_spotify_requests[user_id] = {
                "url": spotify_url,
                "chat_id": chat_id,
            }
            await update.message.reply_text(
                build_spotify_prompt_text(spotify_url),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Continue", callback_data="spotify_confirm"),
                        InlineKeyboardButton("Cancel", callback_data="spotify_cancel"),
                    ],
                ]),
                disable_web_page_preview=True,
            )

        elif is_hentai_playlist_url(extract_http_url(text)):
            playlist_url = extract_http_url(text)
            try:
                playlist = await resolve_hentai_playlist(playlist_url)
                if not playlist.urls:
                    raise RuntimeError("No episode links found on this playlist page.")
            except Exception as exc:
                await update.message.reply_text(
                    (
                        f"{ICON_FAIL} Playlist detection failed.\n\n"
                        f"Reason:\n{shorten(str(exc), 900)}"
                    ),
                    reply_markup=build_reply_menu(user_id),
                    disable_web_page_preview=True,
                )
                return

            pending_hentai_playlist_requests[user_id] = {
                "playlist": playlist,
                "chat_id": chat_id,
            }
            await update.message.reply_text(
                build_hentai_playlist_prompt_text(playlist),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Download all", callback_data="hentai_playlist_confirm"),
                        InlineKeyboardButton("Cancel", callback_data="hentai_playlist_cancel"),
                    ],
                ]),
                disable_web_page_preview=True,
            )

        elif is_video_url(text):
            video_url = extract_http_url(text)
            pending_ytdlp_requests[user_id] = {
                "url": video_url,
                "chat_id": chat_id,
            }
            platform = video_platform_label(video_url)
            target_folder = (
                HENTAI_VIDEO_DIR / video_platform_slug(video_url)
                if is_hentai_video_url(video_url)
                else ADULT_VIDEO_DIR / video_platform_slug(video_url)
                if is_adult_video_url(video_url)
                else DOWNLOAD_DIR
            )

            await update.message.reply_text(
                f"{ICON_DOWNLOAD} {platform} link detected\n\n"
                "Choose download format:\n\n"
                f"Folder:\n{target_folder}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🎬 Video", callback_data="ytdlp_video"),
                        InlineKeyboardButton("🎵 MP3", callback_data="ytdlp_mp3"),
                    ],
                ]),
                disable_web_page_preview=True,
            )

        elif is_direct_http_download_url(text):
            direct_url = extract_http_url(text)
            name = extract_http_filename(direct_url)
            if is_duplicate_name(name):
                await update.message.reply_text(
                    f"{ICON_WARN} {get_lang(user_id, 'duplicate_detected')} {name}",
                    reply_markup=build_reply_menu(user_id)
                )
                return

            job = await start_aria2_download(context.application, chat_id, direct_url, user_id)

            await update.message.reply_text(
                build_download_started_text(job),
                reply_markup=build_reply_menu(user_id),
                disable_web_page_preview=True,
            )
            await update_status_message(context.application, chat_id, user_id)

        elif normalized in ("zip menu", "منوی فشرده‌سازی", "zip", "📦 zip menu"):
            await update.message.reply_text(
                build_zip_menu_text(user_id),
                reply_markup=build_zip_menu_markup(user_id),
            )

        elif normalized in ("tpb search", "جستجوی tpb"):
            await update.message.reply_text(
                f"🏴‍☠️ {get_lang(user_id, 'tpb_welcome')}\n\n"
                f"{get_lang(user_id, 'tpb_send_query')}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            text=get_lang(user_id, 'home_btn'),
                        ),
                    ],
                ]),
                disable_web_page_preview=True,
            )
            context.user_data["tpb_waiting_for_query"] = True

        # Check if user is in TPB search flow (handled before unknown input)
        elif context.user_data.get("tpb_waiting_for_query"):
            context.user_data.pop("tpb_waiting_for_query", None)
            # Clean up any previous TPB result messages
            old_ids = context.user_data.pop("tpb_result_ids", [])
            for mid in old_ids:
                try:
                    await context.bot.delete_message(chat_id, mid)
                except Exception:
                    pass
            if text:
                await update.message.reply_text(
                    f"🔍 {get_lang(user_id, 'select_category').format(text)}",
                    reply_markup=tpb_categories_keyboard(text),
                    disable_web_page_preview=True,
                )
            else:
                await update.message.reply_text(
                    get_lang(user_id, 'tpb_send_query'),
                )
                context.user_data["tpb_waiting_for_query"] = True

        else:
            await update.message.reply_text(
                get_lang(user_id, 'unknown_input'),
                reply_markup=build_reply_menu(user_id),
            )

    except FileNotFoundError:
        await update.message.reply_text(
            f"{ICON_FAIL} {ARIA2_BIN} was not found.",
            reply_markup=build_reply_menu(user_id),
        )
    except (Aria2RpcError, RuntimeError) as e:
        logger.error(f"aria2 error: {type(e).__name__}: {e}", exc_info=e)
        await update.message.reply_text(
            f"{ICON_FAIL} aria2 error:\n{shorten(str(e), 900)}",
            reply_markup=build_reply_menu(user_id),
        )
    except Exception as e:
        # FIX #2: Sanitize error messages to avoid exposing internal details
        logger.error(f"on_text error: {type(e).__name__}: {e}", exc_info=e)
        await update.message.reply_text(
            f"{ICON_FAIL} {get_lang(user_id, 'error_occurred')}",
            reply_markup=build_reply_menu(user_id)
        )

# =========================================================
# Magnet selective handler
# =========================================================
async def on_torrent_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    await update.message.reply_text(f"{ICON_MAGNET} Torrent file received.")

    torrents_dir = DOWNLOAD_DIR / "_torrents"
    torrents_dir.mkdir(exist_ok=True)

    file = await doc.get_file()
    torrent_path = torrents_dir / doc.file_name
    await file.download_to_drive(str(torrent_path))

    return await start_torrent_file_selection(update, context, torrent_path, doc.file_name)


async def start_torrent_file_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    torrent_path: Path,
    title: str | None = None,
):
    files = get_torrent_file_list(torrent_path)

    if not files:
        target = update.callback_query.message if update.callback_query else update.message
        await target.reply_text(f"{ICON_FAIL} Could not read torrent file list.")
        return

    user_id = update.effective_user.id
    torrent_select_sessions[user_id] = {
        "torrent_path": str(torrent_path),
        "files": files,
        "selected": set(),
        "page": 0,
        "title": title or torrent_path.name,
    }

    target = update.callback_query.message if update.callback_query else update.message
    await target.reply_text(
        f"{ICON_MAGNET} Select files to download:\n\n{shorten(clean_download_name(title or torrent_path.name), 90)}",
        reply_markup=build_torrent_select_keyboard(user_id, 0),
    )

def get_torrent_file_list(torrent_path: Path):
    try:
        result = subprocess.run(
            [ARIA2_BIN, "--show-files=true", str(torrent_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
        )
    except Exception:
        return []

    files = []
    for line in result.stdout.splitlines():
        if "|" in line and line.strip()[0].isdigit():
            idx, path = line.split("|", 1)
            files.append({
                "index": idx.strip(),
                "path": path.strip()
            })
    return files

def build_torrent_select_keyboard(user_id: int, page: int):
    session = torrent_select_sessions[user_id]
    files = session["files"]
    selected = session["selected"]

    per_page = 8
    start = page * per_page
    end = start + per_page
    shown = files[start:end]

    rows = []

    for f in shown:
        idx = f["index"]
        checked = "✅" if idx in selected else "⬜"
        name = Path(f["path"]).name
        rows.append([
            InlineKeyboardButton(
                f"{checked} {idx}. {shorten(name, 32)}",
                callback_data=f"tsel:{idx}"
            )
        ])

    rows.append([
        InlineKeyboardButton("✅ Download Selected", callback_data="tconfirm"),
        InlineKeyboardButton("📦 Download All", callback_data="tall"),
    ])

    rows.append([
        InlineKeyboardButton("❌ Cancel", callback_data="tcancel")
    ])

    return InlineKeyboardMarkup(rows)


# =========================================================
# Callback handler
# =========================================================

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    try:
        if data == "menu_home":
            await safe_edit_message(query.message, build_home_text(user_id), build_reply_menu(user_id))
        
        elif data == "refresh_status":
            await update_status_message(context.application, chat_id, user_id)
        
        elif data == "refresh_dashboard":
            await update_live_dashboard(context.application, chat_id, user_id)

        elif data == "menu:downloads":
            await safe_edit_message(
                query.message,
                build_downloads_menu_text(user_id),
                build_downloads_menu_markup(user_id),
            )

        elif data == "menu:files":
            await safe_edit_message(
                query.message,
                build_files_menu_text(user_id),
                build_files_menu_markup(user_id),
            )

        elif data == "menu:tools":
            await safe_edit_message(
                query.message,
                build_tools_menu_text(user_id),
                build_tools_menu_markup(user_id),
            )

        elif data == "menu:settings":
            await safe_edit_message(
                query.message,
                build_settings_menu_text(user_id),
                build_settings_menu_markup(user_id),
            )

        elif data == "menu:status":
            await safe_edit_message(
                query.message,
                build_status_text(user_id),
                build_status_controls_markup(),
            )
            status_messages[chat_id] = {
                "message_id": query.message.message_id,
                "last_update": time.time(),
                "user_id": user_id,
            }

        elif data == "menu:file_browser":
            await safe_edit_message(
                query.message,
                build_files_text("", 0),
                build_files_markup("", 0),
            )

        elif data == "menu:zip":
            await safe_edit_message(
                query.message,
                build_zip_menu_text(user_id),
                build_zip_menu_markup(user_id),
            )

        elif data == "menu:zip_settings":
            await safe_edit_message(
                query.message,
                build_zip_settings_text(user_id),
                build_zip_settings_markup(user_id),
            )

        elif data == "menu:manga_settings":
            await safe_edit_message(
                query.message,
                build_manga_settings_text(user_id),
                build_manga_settings_markup(user_id),
            )

        elif data == "menu:language":
            await safe_edit_message(
                query.message,
                f"{ICON_LANGUAGE} {get_lang(user_id, 'language')}\n\n{get_lang(user_id, 'select_language')}",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(get_lang_for_all("en", "en"), callback_data="set_lang:en"),
                    InlineKeyboardButton(get_lang_for_all("fa", "fa"), callback_data="set_lang:fa"),
                ]]),
            )

        elif data == "menu:forwarded_posts":
            settings = get_user_settings(user_id)
            enabled = not bool(settings.get("auto_download_forwarded_posts"))
            await update_setting(user_id, "auto_download_forwarded_posts", enabled)
            await safe_edit_message(
                query.message,
                build_settings_menu_text(user_id),
                build_settings_menu_markup(user_id),
            )
            await query.answer(f"Forwarded posts: {'ON' if enabled else 'OFF'}")

        elif data == "menu:clear":
            await safe_edit_message(
                query.message,
                f"{ICON_WARN} Clear finished jobs from memory?",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"{ICON_BROOM} Yes, Clear", callback_data="clear_confirm"),
                    InlineKeyboardButton(f"{ICON_BACK} Back", callback_data="menu:downloads"),
                ]]),
            )

        elif data == "menu:tpb":
            context.user_data["tpb_waiting_for_query"] = True
            await safe_edit_message(
                query.message,
                f"{ICON_MAGNET} {get_lang(user_id, 'tpb_welcome')}\n\n"
                f"{get_lang(user_id, 'tpb_send_query')}",
            )
        
        elif data.startswith("set_lang:"):
            lang = data.split(":")[1]
            if lang in ("en", "fa"):
                user_languages[user_id] = lang
                await safe_edit_message(
                    query.message, 
                    build_home_text(user_id), 
                    build_reply_menu(user_id)
                )
            else:
                await query.answer("Invalid language", show_alert=True)
        
        elif data.startswith("cancel_confirm:"):
            jid = int(data.split(":")[1])
            ok, msg = await cancel_job(jid)
            if ok:
                await safe_edit_message(
                    query.message,
                    f"{ICON_OK} {msg}",
                )
            else:
                await query.answer(msg, show_alert=True)

        elif data.startswith("job_cancel:"):
            jid = int(data.split(":")[1])
            ok, msg = await cancel_job(jid)
            await update_status_message(context.application, chat_id, user_id)
            await query.answer(msg, show_alert=not ok)

        elif data.startswith("job_pause:"):
            jid = int(data.split(":")[1])
            ok, msg = await pause_job(jid)
            await update_status_message(context.application, chat_id, user_id)
            await query.answer(msg, show_alert=not ok)

        elif data.startswith("job_resume:"):
            jid = int(data.split(":")[1])
            ok, msg = await resume_job(jid)
            await update_status_message(context.application, chat_id, user_id)
            await query.answer(msg, show_alert=not ok)

        elif data == "manga_confirm":
            pending = pending_manga_requests.pop(user_id, None)
            if not pending:
                await safe_edit_message(query.message, f"{ICON_WARN} No pending manga link found.")
                return

            job = await start_manga_download(
                context.application,
                pending["chat_id"],
                pending["url"],
                user_id,
            )
            await safe_edit_message(
                query.message,
                build_manga_started_text(job),
            )
            await update_status_message(context.application, chat_id, user_id)

        elif data == "manga_cancel":
            pending_manga_requests.pop(user_id, None)
            await safe_edit_message(
                query.message,
                f"{ICON_STOP} Manga download cancelled.",
            )

        elif data.startswith("manga_setting:"):
            setting_key = data.split(":", 1)[1]
            setting_name = {
                "auto_convert": "manga_auto_convert_pdf",
                "remove_images": "manga_remove_images_after_conversion",
            }.get(setting_key)
            if not setting_name:
                await query.answer("Unknown manga setting", show_alert=True)
                return
            settings = get_user_settings(user_id)
            await update_setting(user_id, setting_name, not settings.get(setting_name, False))
            await safe_edit_message(
                query.message,
                build_manga_settings_text(user_id),
                build_manga_settings_markup(user_id),
            )

        elif data == "spotify_confirm":
            pending = pending_spotify_requests.pop(user_id, None)
            if not pending:
                await safe_edit_message(query.message, f"{ICON_WARN} No pending Spotify link found.")
                return

            job = await start_spotify_download(
                context.application,
                pending["chat_id"],
                pending["url"],
                user_id,
            )
            await safe_edit_message(
                query.message,
                build_spotify_started_text(job),
            )
            await update_status_message(context.application, chat_id, user_id)

        elif data == "spotify_cancel":
            pending_spotify_requests.pop(user_id, None)
            await safe_edit_message(
                query.message,
                f"{ICON_STOP} Spotify download cancelled.",
            )
        
        elif data == "clear_confirm":
            removed = clear_finished_jobs()
            await safe_edit_message(
                query.message,
                f"{ICON_OK} {get_lang(user_id, 'cleared')}: {removed} {get_lang(user_id, 'job_id')}(s)",
            )
        
        # ===== Torrent Selection =====
        elif data.startswith("tsel:"):
            idx = data.split(":")[1]
            session = torrent_select_sessions[user_id]

            if idx in session["selected"]:
                session["selected"].remove(idx)
            else:
                session["selected"].add(idx)

            await query.edit_message_reply_markup(
                build_torrent_select_keyboard(user_id, session["page"])
            )

        elif data == "tconfirm":
            session = torrent_select_sessions.pop(user_id)

            selected = sorted(session["selected"], key=int)

            if not selected:
                await query.answer(get_lang(user_id, 'select_at_least'), show_alert=True)
                return

            await start_aria2_download(
                context.application,
                chat_id,
                session["torrent_path"] + f" --select-file={','.join(selected)}",
                user_id,
            )
            await update_status_message(context.application, chat_id, user_id)

            await query.edit_message_text(f"{ICON_SPEED} {get_lang(user_id, 'preparing')}...")

        elif data == "tall":
            session = torrent_select_sessions.pop(user_id)

            await start_aria2_download(
                context.application,
                chat_id,
                session["torrent_path"],
                user_id,
            )
            await update_status_message(context.application, chat_id, user_id)

            await query.edit_message_text(f"{ICON_SPEED} {get_lang(user_id, 'preparing')}...")

        elif data == "tcancel":
            torrent_select_sessions.pop(user_id, None)
            await query.edit_message_text(get_lang(user_id, 'cancelled'))

        # ===== File Browser =====
        elif data.startswith("fb:"):
            parts = data.split(":", 3)
            action = parts[1]

            if action == "list":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""

                await safe_edit_message(
                    query.message,
                    build_files_text(rel_path, page),
                    build_files_markup(rel_path, page),
                )

            elif action == "dir":
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""

                await safe_edit_message(
                    query.message,
                    build_files_text(rel_path, 0),
                    build_files_markup(rel_path, 0),
                )

            elif action == "dirinfo":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""

                await safe_edit_message(
                    query.message,
                    build_folder_details_text(rel_path),
                    build_folder_details_markup(rel_path, page),
                )

            elif action == "manga_pdf":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                folder = safe_join(DOWNLOAD_DIR, rel_path)
                if not is_manga_gallery_folder(folder):
                    await query.answer("No manga images found in this folder.", show_alert=True)
                    return

                await safe_edit_message(
                    query.message,
                    f"{ICON_IMAGE} Converting manga folder to PDF...\n\n{folder.name}",
                )
                pdf_path = await convert_manga_folder_to_pdf_job(folder, user_id)
                await safe_edit_message(
                    query.message,
                    f"{ICON_OK} Manga PDF created\n\n{pdf_path.name}\n\nSaved in:\n{DOWNLOAD_DIR}",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{ICON_FOLDER} Folder", callback_data=f"fb:dirinfo:{page}:{encoded}")],
                        [InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")],
                    ]),
                )

            elif action == "file":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""

                await safe_edit_message(
                    query.message,
                    build_file_details_text(rel_path),
                    build_file_details_markup(rel_path, page),
                )

            elif action == "delete_confirm":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""

                await safe_edit_message(
                    query.message,
                    build_delete_confirm_text(rel_path),
                    build_delete_confirm_markup(rel_path, page),
                )

            elif action == "delete_yes":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""

                deleted_kind = delete_path(rel_path)
                parent = rel_parent(rel_path)
                parent_encoded = encode_path(parent)

                await safe_edit_message(
                    query.message,
                    (
                        f"{ICON_OK} Deleted successfully\n\n"
                        f"Type: {deleted_kind}\n"
                        f"Path: /{rel_path.lstrip('/')}"
                    ),
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(f"{ICON_BACK} Back", callback_data=f"fb:list:{page}:{parent_encoded}"),
                            InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:"),
                        ],
                    ]),
                )

            elif action == "send_confirm":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""

                info = file_info(rel_path)
                if info["is_dir"]:
                    raise IsADirectoryError(rel_path)

                await safe_edit_message(
                    query.message,
                    (
                        f"{ICON_UPLOAD} Upload File\n\n"
                        f"Name: {info['name']}\n"
                        f"{ICON_PIN} Path: /{rel_path.lstrip('/')}\n"
                        f"{ICON_BOX} Size: {human_size(info['size'])}\n\n"
                        "Target: your own Telegram account\n\n"
                        "Upload this file?"
                    ),
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{ICON_OK} Yes, Upload", callback_data=f"fb:send_yes:{page}:{encoded}")],
                        [InlineKeyboardButton(f"{ICON_BACK} Cancel", callback_data=f"fb:file:{page}:{encoded}")],
                    ]),
                )

            elif action == "send_yes":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                parent_encoded = encode_path(rel_parent(rel_path))

                await safe_edit_message(
                    query.message,
                    f"{ICON_UPLOAD} Preparing upload...\nPlease wait.",
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(f"{ICON_BACK} Back", callback_data=f"fb:list:{page}:{parent_encoded}"),
                            InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:"),
                        ]
                    ]),
                )

                await send_single_file_via_pyrogram(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    rel_path,
                    user_id=user_id,
                )

            elif action == "send_folder_confirm":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""

                info = folder_info(rel_path)
                files = get_all_files_in_folder(rel_path)

                await safe_edit_message(
                    query.message,
                    (
                        f"{ICON_UPLOAD} Upload All Files\n\n"
                        f"Folder: {info['name']}\n"
                        f"{ICON_PIN} Path: /{rel_path.lstrip('/') if rel_path else ''}\n"
                        f"Files found: {len(files)}\n"
                        f"{ICON_BOX} Total size: {human_size(info['total_size'])}\n"
                        "Target: your own Telegram account\n\n"
                        "Upload all files from this folder?"
                    ),
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{ICON_OK} Yes, Upload All", callback_data=f"fb:send_folder_yes:{page}:{encoded}")],
                        [InlineKeyboardButton(f"{ICON_BACK} Cancel", callback_data=f"fb:dirinfo:{page}:{encoded}")],
                    ]),
                )

            elif action == "send_folder_yes":
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""

                await safe_edit_message(
                    query.message,
                    f"{ICON_UPLOAD} Preparing folder upload...\nPlease wait.",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")]
                    ]),
                )

                await send_folder_files_via_pyrogram(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    rel_path,
                    user_id=user_id,
                )
            
            # NEW: Inline Upload Confirmation
            elif action == "upload_file_confirm":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                info = file_info(rel_path)
                await safe_edit_message(
                    query.message,
                    (
                        f"{ICON_UPLOAD} Upload File\n\n"
                        f"Name: {info['name']}\n"
                        f"Size: {human_size(info['size'])}\n\n"
                        "Upload this file?"
                    ),
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{ICON_OK} Yes", callback_data=f"fb:upload_file_yes:{page}:{encoded}")],
                        [InlineKeyboardButton(f"{ICON_BACK} Cancel", callback_data=f"fb:list:{page}:{encode_path(rel_parent(rel_path))}")]
                    ])
                )
            elif action == "upload_file_yes":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                parent_encoded = encode_path(rel_parent(rel_path))
                await safe_edit_message(
                    query.message,
                    f"{ICON_UPLOAD} Preparing upload...\nPlease wait.",
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{ICON_BACK} Back", callback_data=f"fb:list:{page}:{parent_encoded}")]])
                )
                await send_single_file_via_pyrogram(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    rel_path,
                    user_id=user_id,
                )
            
            # NEW: Inline Delete Confirmation
            elif action == "delete_file_confirm":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                info = file_info(rel_path)
                await safe_edit_message(
                    query.message,
                    (
                        f"{ICON_WARN} Confirm Delete\n\n"
                        f"Name: {info['name']}\n"
                        f"Size: {human_size(info['size'])}\n\n"
                        f"Delete this file permanently?"
                    ),
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{ICON_DELETE} Yes", callback_data=f"fb:delete_file_yes:{page}:{encoded}")],
                        [InlineKeyboardButton(f"{ICON_BACK} Cancel", callback_data=f"fb:list:{page}:{encode_path(rel_parent(rel_path))}")]
                    ])
                )
            elif action == "delete_file_yes":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                delete_path(rel_path)
                parent = rel_parent(rel_path)
                parent_encoded = encode_path(parent)
                await safe_edit_message(
                    query.message,
                    f"{ICON_OK} Deleted: {rel_path}",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{ICON_BACK} Back", callback_data=f"fb:list:{page}:{parent_encoded}")],
                    ])
                )

            elif action == "batch":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""

                batch_select_sessions[user_id] = {
                    "rel_path": rel_path,
                    "selected": set(),
                    "page": page,
                    "mode": "upload",
                }

                await safe_edit_message(
                    query.message,
                    build_batch_select_text(rel_path, page, user_id),
                    build_batch_select_markup(rel_path, user_id, page),
                )

            elif action == "batchdel":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""

                batch_select_sessions[user_id] = {
                    "rel_path": rel_path,
                    "selected": set(),
                    "page": page,
                    "mode": "delete",
                }

                await safe_edit_message(
                    query.message,
                    build_batch_select_text(rel_path, page, user_id),
                    build_batch_select_markup(rel_path, user_id, page),
                )

            elif action == "deleteall_confirm":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                entries = list_dir(rel_path)
                file_count = sum(1 for e in entries if not e["is_dir"])
                folder_count = sum(1 for e in entries if e["is_dir"])
                shown_path = "/" + rel_path.lstrip("/") if rel_path else "/"

                await safe_edit_message(
                    query.message,
                    (
                        f"{ICON_WARN} {get_lang(user_id, 'delete_all_confirm')}\n\n"
                        f"{ICON_PIN} {shown_path}\n"
                        f"Files: {file_count}\n"
                        f"Folders: {folder_count}\n\n"
                        f"{get_lang(user_id, 'delete_all_warning')}"
                    ),
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                get_lang(user_id, "yes_delete_all"),
                                callback_data=f"fb:deleteall_yes:{page}:{encoded}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                get_lang(user_id, "cancel_btn"),
                                callback_data=f"fb:list:{page}:{encoded}",
                            ),
                            InlineKeyboardButton(
                                get_lang(user_id, "home_btn"),
                            ),
                        ],
                    ]),
                )

            elif action == "deleteall_yes":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                parent_encoded = encode_path(rel_path)

                files_n, folders_n, errors = delete_all_in_directory(rel_path)
                text = get_lang(user_id, "delete_all_done").format(files_n, folders_n)
                if errors:
                    preview = "\n".join(errors[:5])
                    if len(errors) > 5:
                        preview += f"\n... and {len(errors) - 5} more"
                    text += f"\n\n{ICON_WARN} Errors:\n{preview}"

                await safe_edit_message(
                    query.message,
                    f"{ICON_OK} {text}",
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                get_lang(user_id, "back"),
                                callback_data=f"fb:list:{page}:{parent_encoded}",
                            ),
                            InlineKeyboardButton(
                                get_lang(user_id, "root"),
                                callback_data="fb:list:0:",
                            ),
                        ],
                    ]),
                )

            elif action == "organize":
                # Run the organizer script in background without blocking
                encoded = parts[3] if len(parts) > 3 else ""
                # acknowledge the button press
                await query.answer()

                script_path = BASE_DIR / "scripts" / "organize_downloaded_videos.py"

                loop = asyncio.get_running_loop()

                def _run():
                    try:
                        subprocess.run([sys.executable, str(script_path)], cwd=str(BASE_DIR))
                    except Exception:
                        logger.exception("organize_downloaded_videos.py failed")

                loop.run_in_executor(None, _run)

            elif action == "blist":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                user_id = query.from_user.id
                
                if user_id in batch_select_sessions:
                    batch_select_sessions[user_id]["page"] = page
                
                await safe_edit_message(
                    query.message,
                    build_batch_select_text(rel_path, page, user_id),
                    build_batch_select_markup(rel_path, user_id, page),
                )

            elif action == "bselect":
                encoded = parts[2] if len(parts) > 2 else ""
                rel_path = decode_path(encoded) if encoded else ""

                if user_id not in batch_select_sessions:
                    return

                session = batch_select_sessions[user_id]
                if rel_path in session["selected"]:
                    session["selected"].remove(rel_path)
                else:
                    session["selected"].add(rel_path)

                await safe_edit_message(
                    query.message,
                    build_batch_select_text(session["rel_path"], session["page"], user_id),
                    build_batch_select_markup(session["rel_path"], user_id, session["page"]),
                )

            elif action == "bdelete_confirm":
                if user_id not in batch_select_sessions:
                    await query.answer("Session expired", show_alert=True)
                    return

                session = batch_select_sessions[user_id]
                selected = list(session["selected"])
                if not selected:
                    await query.answer(get_lang(user_id, "select_at_least"), show_alert=True)
                    return

                total_size = 0
                names = []
                for rel in selected:
                    try:
                        info = file_info(rel)
                        total_size += info["size"]
                        names.append(info["name"])
                    except Exception:
                        names.append(rel)

                preview = "\n".join(f"• {n}" for n in names[:12])
                if len(names) > 12:
                    preview += f"\n... and {len(names) - 12} more"

                await safe_edit_message(
                    query.message,
                    (
                        f"{ICON_WARN} {get_lang(user_id, 'batch_delete_confirm')}\n\n"
                        f"Files: {len(selected)}\n"
                        f"{ICON_BOX} Total: {human_size(total_size)}\n\n"
                        f"{preview}\n\n"
                        f"{get_lang(user_id, 'delete_warning_file')}"
                    ),
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            get_lang(user_id, "yes_batch_delete"),
                            callback_data="fb:bdelete_yes",
                        )],
                        [InlineKeyboardButton(
                            get_lang(user_id, "cancel_btn"),
                            callback_data=f"fb:batchdel:{session['page']}:{encode_path(session['rel_path'])}",
                        )],
                    ]),
                )

            elif action == "bdelete_yes":
                if user_id not in batch_select_sessions:
                    await query.answer("Session expired", show_alert=True)
                    return

                session = batch_select_sessions.pop(user_id)
                selected = list(session["selected"])
                rel_path = session["rel_path"]
                page = session["page"]
                parent_encoded = encode_path(rel_path)

                deleted, errors = delete_paths_batch(selected)
                text = get_lang(user_id, "deleted_count").format(deleted)
                if errors:
                    preview = "\n".join(errors[:5])
                    if len(errors) > 5:
                        preview += f"\n... and {len(errors) - 5} more"
                    text += f"\n\n{ICON_WARN} Errors:\n{preview}"

                await safe_edit_message(
                    query.message,
                    f"{ICON_OK} {text}",
                    InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                get_lang(user_id, "back"),
                                callback_data=f"fb:list:{page}:{parent_encoded}",
                            ),
                            InlineKeyboardButton(
                                get_lang(user_id, "root"),
                                callback_data="fb:list:0:",
                            ),
                        ],
                    ]),
                )

            elif action == "bupload":
                user_id = query.from_user.id
                if user_id not in batch_select_sessions:
                    await query.answer("Session expired", show_alert=True)
                    return
                
                session = batch_select_sessions.pop(user_id)
                selected = list(session["selected"])
                
                if not selected:
                    await query.answer("Select at least one file", show_alert=True)
                    return
                
                await safe_edit_message(
                    query.message,
                    f"{ICON_UPLOAD} Preparing batch upload ({len(selected)} files)...\nPlease wait.",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")]
                    ]),
                )
                
                await send_folder_files_via_pyrogram(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    session["rel_path"],
                    file_list=selected,
                    user_id=user_id,
                )
            
            elif action == "bupload_empty":
                await query.answer(get_lang(user_id, "select_at_least"), show_alert=True)

            # New: Video conversion menu
            elif action == "conv_menu":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                # Show resolution options
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("1080p", callback_data=f"fb:conv_start:{page}:{encoded}:1080p")],
                    [InlineKeyboardButton("720p",  callback_data=f"fb:conv_start:{page}:{encoded}:720p")],
                    [InlineKeyboardButton("480p",  callback_data=f"fb:conv_start:{page}:{encoded}:480p")],
                    [InlineKeyboardButton("360p",  callback_data=f"fb:conv_start:{page}:{encoded}:360p")],
                    [InlineKeyboardButton("❌ Cancel", callback_data=f"fb:file:{page}:{encoded}")]
                ])
                await safe_edit_message(query.message, "Select target resolution:", keyboard)

            elif action == "conv_start":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                target_res = parts[4] if len(parts) > 4 else "720p"
                rel_path = decode_path(encoded) if encoded else ""
                input_path = safe_join(DOWNLOAD_DIR, rel_path)
                if not input_path.exists() or not is_video_file(str(input_path)):
                    await query.edit_message_text("❌ Not a valid video file.")
                    return

                # Prepare output filename
                stem = input_path.stem
                ext = input_path.suffix
                output_path = input_path.parent / f"{stem}_{target_res}{ext}"
                temp_output = output_path
                # Avoid re‑converting if already exists
                if temp_output.exists():
                    await query.edit_message_text(
                        f"⏩ Converted file already exists: {temp_output.name}\nDo you want to upload it?",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("📤 Upload", callback_data=f"fb:conv_upload:{page}:{encoded}:{encode_path(str(temp_output.relative_to(DOWNLOAD_DIR)))}")],
                            [InlineKeyboardButton("🗑 Delete & Re-convert", callback_data=f"fb:conv_start:{page}:{encoded}:{target_res}")],
                            [InlineKeyboardButton("🔙 Back", callback_data=f"fb:file:{page}:{encoded}")]
                        ])
                    )
                    return

                # Show progress message
                msg = await query.edit_message_text(f"🔄 Converting to {target_res}... 0%")
                async def progress_callback(pct):
                    try:
                        await msg.edit_text(f"🔄 Converting to {target_res}... {pct}%")
                    except:
                        pass
                # Run conversion in executor
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, lambda: asyncio.run(convert_video_quality(str(input_path), str(temp_output), target_res, progress_callback)))
                # After conversion
                await msg.edit_text(
                    f"✅ Conversion finished!\nOutput: {temp_output.name}\nSize: {human_size(temp_output.stat().st_size)}",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📤 Upload converted file", callback_data=f"fb:conv_upload:{page}:{encoded}:{encode_path(str(temp_output.relative_to(DOWNLOAD_DIR)))}")],
                        [InlineKeyboardButton("🔙 Back to file", callback_data=f"fb:file:{page}:{encoded}")]
                    ])
                )

            elif action == "conv_upload":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                original_encoded = parts[3] if len(parts) > 3 else ""
                converted_encoded = parts[4] if len(parts) > 4 else ""
                converted_rel = decode_path(converted_encoded) if converted_encoded else ""
                await safe_edit_message(query.message, f"{ICON_UPLOAD} Uploading converted file...")
                await send_single_file_via_pyrogram(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    converted_rel,
                    user_id=user_id,
                )

            # New: Send thumbnail (multiple frames)
            elif action == "thumb_send":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                await send_thumbnail(update, context, rel_path)

        # ===== Zip Menu =====
        elif data.startswith("zip_menu:"):
            action = data.split(":")[1]
            
            if action == "list":
                page = int(data.split(":")[2]) if len(data.split(":")) > 2 else 0
                text, markup = build_zip_file_list_markup(user_id, page)
                await safe_edit_message(query.message, text, markup)
            
            elif action == "select":
                page = int(data.split(":")[2]) if len(data.split(":")) > 2 else 0
                text, markup = build_zip_file_select_markup(user_id, page)
                await safe_edit_message(query.message, text, markup)
            
            elif action == "zip_all":
                try:
                    all_files = filter_files_for_archiving(collect_download_files())

                    if not all_files:
                        await query.answer(get_lang(user_id, 'no_files'), show_alert=True)
                        return

                    settings = get_user_settings(user_id)
                    all_files, limit_warn = apply_zip_file_limit(all_files)
                    files_to_zip = build_files_to_zip(all_files)
                    source_files = list(all_files)

                    # Store session info and ask for zip name
                    pending_zip_name_sessions[user_id] = {
                        "mode": "all",
                        "files_to_zip": files_to_zip,
                        "source_files": source_files,
                        "settings": settings,
                        "limit_warn": limit_warn,
                        "message_id": query.message.message_id,
                    }

                    await query.edit_message_text(
                        get_lang(user_id, 'enter_zip_name'),
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(f"{ICON_BACK} Cancel", callback_data="zip_menu:back")]
                        ])
                    )

                except Exception as e:
                    logger.error(f"Zip error: {e}")
                    await query.edit_message_text(f"{clean_emoji_prefix(get_lang(user_id, 'zip_error'))}: {e}")
            
            elif action == "settings":
                text = build_zip_settings_text(user_id)
                markup = build_zip_settings_markup(user_id)
                await safe_edit_message(query.message, text, markup)
            
            elif action == "back":
                await safe_edit_message(
                    query.message,
                    build_zip_menu_text(user_id),
                    build_zip_menu_markup(user_id)
                )
        
        # ===== Zip File Selection =====
        elif data.startswith("zip_select:"):
            action = data.split(":", 1)[1]
            session = zip_select_sessions.get(user_id)

            if action == "confirm":
                if not session or not session.get("selected"):
                    # FIX #7: Use translated string instead of hardcoded
                    await query.answer(get_lang(user_id, 'select_at_least'), show_alert=True)
                    return

                try:
                    selected_files = resolve_selected_zip_files(session)

                    if not selected_files:
                        await query.answer(get_lang(user_id, 'select_at_least'), show_alert=True)
                        return

                    settings = get_user_settings(user_id)
                    files_to_zip = build_files_to_zip(selected_files)

                    # Store session info and ask for zip name
                    pending_zip_name_sessions[user_id] = {
                        "mode": "selected",
                        "files_to_zip": files_to_zip,
                        "source_files": selected_files,
                        "settings": settings,
                        "message_id": query.message.message_id,
                    }

                    # Remove from selection session after storing
                    zip_select_sessions.pop(user_id, None)

                    await query.edit_message_text(
                        get_lang(user_id, 'enter_zip_name'),
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(f"{ICON_BACK} Cancel", callback_data="zip_menu:back")]
                        ])
                    )

                except Exception as e:
                    logger.error(f"Zip error: {e}")
                    await query.edit_message_text(f"{clean_emoji_prefix(get_lang(user_id, 'zip_error'))}: {e}")

            else:
                if not session:
                    session = {"selected": set(), "page": 0}
                    zip_select_sessions[user_id] = session

                if action not in session.get("selected", set()):
                    session["selected"].add(action)
                else:
                    session["selected"].discard(action)
                
                page = session.get("page", 0)
                text, markup = build_zip_file_select_markup(user_id, page)
                await safe_edit_message(query.message, text, markup)
        
        # ===== Zip Settings =====
        elif data.startswith("zip_setting:"):
            setting_key = data.split(":")[1]
            
            if setting_key == "part_size":
                # Show part size options
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("256 MB", callback_data="zip_set_value:zip_part_size:268435456")],
                    [InlineKeyboardButton("512 MB", callback_data="zip_set_value:zip_part_size:536870912")],
                    [InlineKeyboardButton("1 GB", callback_data="zip_set_value:zip_part_size:1073741824")],
                    [InlineKeyboardButton("2 GB", callback_data="zip_set_value:zip_part_size:2147483648")],
                    [InlineKeyboardButton("🏠 Back", callback_data="zip_menu:settings")],
                ])
                await query.edit_message_reply_markup(keyboard)
            
            elif setting_key == "method":
                # Show zip method options
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("ZIP", callback_data="zip_set_value:zip_method:zip")],
                    [InlineKeyboardButton("7Z", callback_data="zip_set_value:zip_method:7z")],
                    [InlineKeyboardButton("🏠 Back", callback_data="zip_menu:settings")],
                ])
                await query.edit_message_reply_markup(keyboard)
            
            elif setting_key == "password":
                session = zip_select_sessions.get(user_id, {"selected": set(), "page": 0})
                session["waiting_for"] = "password"
                zip_select_sessions[user_id] = session
                # FIX #1: Make password waiting state clearer to user
                await query.edit_message_text(
                    "🔐 <b>Waiting for password input</b>\n\n"
                    "Send your desired password (or send 'none' to remove the password)"
                )
            
            elif setting_key in ("auto_del_files", "auto_del_zips", "auto_del_upload", "forwarded_posts"):
                # Toggle boolean setting
                setting_name = {
                    "auto_del_files": "auto_delete_files_after_zip",
                    "auto_del_zips": "auto_delete_zips_after_send",
                    "auto_del_upload": "auto_delete_files_after_upload",
                    "forwarded_posts": "auto_download_forwarded_posts",
                }.get(setting_key)
                
                if setting_name:
                    settings = get_user_settings(user_id)
                    current_value = settings.get(setting_name, False)
                    await update_setting(user_id, setting_name, not current_value)
                    
                    text = build_zip_settings_text(user_id)
                    markup = build_zip_settings_markup(user_id)
                    await safe_edit_message(query.message, text, markup)
            
            elif setting_key == "compression":
                # Show compression level options
                keyboard = []
                row = []
                for level in range(1, 10):
                    row.append(InlineKeyboardButton(str(level), callback_data=f"zip_set_value:compression_level:{level}"))
                    if level % 3 == 0:
                        keyboard.append(row)
                        row = []
                
                if row:
                    keyboard.append(row)
                keyboard.append([InlineKeyboardButton("🏠 Back", callback_data="zip_menu:settings")])
                
                markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_reply_markup(markup)
        
        elif data.startswith("zip_set_value:"):
            parts = data.split(":")
            setting_name = parts[1]
            value = ":".join(parts[2:])

            if setting_name in ("zip_part_size", "compression_level"):
                try:
                    value = int(value)
                except ValueError:
                    # FIX #7: Use translated string instead of hardcoded
                    await query.answer(get_lang(user_id, 'invalid_value'), show_alert=True)
                    return
            elif setting_name == "zip_method":
                value = value.lower()
                if value not in ("zip", "7z"):
                    await query.answer(get_lang(user_id, 'invalid_archive_method'), show_alert=True)
                    return
                fmt_err = check_archive_format_support(value)
                if fmt_err:
                    await query.answer(fmt_err, show_alert=True)
                    return
            elif setting_name in (
                "auto_delete_files_after_zip",
                "auto_delete_zips_after_send",
                "auto_delete_files_after_upload",
            ):
                value = value.lower() == "true"

            if setting_name == "zip_part_size":
                if not validate_part_size(value // (1024 * 1024)):
                    await query.answer(get_lang(user_id, 'part_size_error'), show_alert=True)
                    return
            elif setting_name == "compression_level":
                if not validate_compression_level(value):
                    # FIX #7: Use translated string instead of hardcoded
                    await query.answer(get_lang(user_id, 'compression_error'), show_alert=True)
                    return

            ok = await update_setting(user_id, setting_name, value)
            if not ok:
                await query.answer(get_lang(user_id, 'invalid_value'), show_alert=True)
                return

            text = build_zip_settings_text(user_id)
            markup = build_zip_settings_markup(user_id)
            await safe_edit_message(query.message, text, markup)

    except Exception as e:
        await safe_edit_message(
            query.message,
            f"{ICON_FAIL} Error\n{type(e).__name__}: {e}",
            InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")],
            ]),
        )



# =========================================================
# Main
# =========================================================

async def post_init(app: Application):
    """Initialize bot - setup pyrogram, executor, and start auto-cleanup task."""
    global zip_executor
    
    # Initialize thread pool executor for zip operations
    # Using ThreadPoolExecutor instead of ProcessPoolExecutor to properly share
    # the progress object across threads. With GIL, this is safe for I/O-bound work.
    loop = asyncio.get_running_loop()
    # Limit to 2 workers to prevent bot overload from concurrent zip operations
    zip_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="zip_worker")
    
    client = await get_pyrogram_client()
    
    # FIX #10: Verify pyrogram is logged in as user account (not bot)
    try:
        me = await client.get_me()
        if me.is_bot:
            raise RuntimeError(
                f"❌ FATAL: Pyrogram is logged in as a BOT (@{me.username}).\n"
                f"You must log in with a PERSONAL USER account instead.\n"
                f"Fix: Delete '{PYRO_SESSION_NAME}.session' and restart the script.\n"
                f"Then log in with your personal Telegram account."
            )
        logger.info(f"✅ Pyrogram logged in as user: @{me.username}")
    except Exception as e:
        logger.warning(f"Could not verify pyrogram user account: {e}")
    
    # FIX #8: Start auto-cleanup task for old files
    async def cleanup_task():
        while True:
            try:
                await asyncio.sleep(3600)  # Run every hour
                await auto_cleanup_old_files()
            except Exception as e:
                logger.error(f"Auto-cleanup error: {e}")
    
    asyncio.create_task(cleanup_task())
    logger.info("✅ Auto-cleanup task started")


    if WEB_APP_ENABLE and WEB_APP_URL:
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Mini-App",
                    web_app=WebAppInfo(url=WEB_APP_URL),
                )
            )
            logger.info("Telegram mini-app menu button configured")
        except Exception as exc:
            logger.warning("Unable to configure Telegram mini-app menu button: %s", exc)


async def post_shutdown(app: Application):
    """Cleanup on shutdown - stop pyrogram client and shutdown executor."""
    global zip_executor
    
    # Shutdown zip executor gracefully
    if zip_executor:
        zip_executor.shutdown(wait=True)
        zip_executor = None
    
    stop_dashboard_server(getattr(app, "dashboard_server", None))
    await stop_pyrogram_client()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"Telegram network error: {err}")
        return
    logger.error(f"Telegram error: {err}", exc_info=err)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")
    if not API_ID or not API_HASH:
        raise RuntimeError("API_ID or API_HASH missing")

    # Configure HTTP request with proper timeouts
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    async def _wrapped_post_init(app_ref: Application):
        await post_init(app_ref)

        # Start Pyrogram eagerly and register forwarded-media handler
        try:
            client = await get_pyrogram_client()
            setup_pyrogram_forwarded_downloads(client, str(TELEGRAM_DIR))
        except Exception as exc:
            logger.warning("Pyrogram forwarded-media handler not registered: %s", exc)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(_wrapped_post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    if WEB_DASHBOARD_ENABLE:
        try:
            app.dashboard_server = start_dashboard_server(
                WEB_DASHBOARD_HOST,
                WEB_DASHBOARD_PORT,
                download_jobs,
                upload_jobs,
            )
            logger.info(
                "✅ Web dashboard started at http://%s:%s",
                WEB_DASHBOARD_HOST,
                WEB_DASHBOARD_PORT,
            )
        except Exception as exc:
            logger.warning("Unable to start web dashboard: %s", exc)

    # Core commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("browse", browse_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("mangasettings", manga_settings_cmd))
    app.add_handler(CommandHandler("forwardedposts", forwarded_posts_cmd))
    app.add_handler(CommandHandler("autoforward", forwarded_posts_cmd))

    # Zipping feature handlers
    app.add_handler(CommandHandler("zip", zip_files_cmd))
    app.add_handler(CommandHandler("list", list_files_cmd))
    app.add_handler(CommandHandler("clear", clear_files_cmd))

    # TPB crawler handlers
    tpb_crawler = TPBCrawler(TPB_API_URL)

    async def torrent_search_start_download(update, context, source: str):
        """Wrapper to start aria2 downloads from torrent search providers."""
        source_value, _ = _split_torrent_source(source)
        if _is_local_torrent_file(source_value):
            name = clean_download_name(Path(source_value).name)
        elif source_value.lower().startswith(("http://", "https://")):
            name = extract_http_filename(source_value)
        else:
            name = extract_bt_name(source_value)
        if is_duplicate_name(name):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ Duplicate detected: {name}",
            )
            return {"id": 0, "name": name, "status": "duplicate"}
        job = await start_aria2_download(
            context.application,
            update.effective_chat.id,
            source,
            update.effective_user.id if update.effective_user else None,
        )
        await update_status_message(context.application, update.effective_chat.id, update.effective_user.id if update.effective_user else None)
        return job

    tpb_handlers = TPBHandlers(
        tpb_crawler,
        lang_func=get_lang,
        download_func=torrent_search_start_download,
    )
    app.add_handler(CommandHandler("tpb", tpb_handlers.tpb_cmd))
    app.add_handler(CommandHandler("tpbget", tpb_handlers.tpb_get_cmd))
    app.add_handler(CallbackQueryHandler(tpb_handlers.category_callback, pattern=r"^tpb_cat_"))
    app.add_handler(CallbackQueryHandler(tpb_handlers.page_callback, pattern=r"^tpb_page_"))
    app.add_handler(CallbackQueryHandler(tpb_handlers.newsearch_callback, pattern=r"^tpb_newsearch$"))
    app.add_handler(CallbackQueryHandler(tpb_handlers.magnet_callback, pattern=r"^tpb_magnet_"))
    app.add_handler(CallbackQueryHandler(tpb_handlers.download_callback, pattern=r"^tpb_dl_"))
    app.add_handler(CallbackQueryHandler(tpb_handlers.info_callback, pattern=r"^tpb_info_"))


    # Legacy handlers
    app.add_handler(CallbackQueryHandler(handle_ytdlp_callback, pattern=r"^ytdlp_"))
    app.add_handler(CallbackQueryHandler(handle_hentai_playlist_callback, pattern=r"^hentai_playlist_"))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.FileExtension("torrent"), on_torrent_file))

    # Forwarded media is handled natively by Pyrogram (see _wrapped_post_init)
    # DO NOT register a PTB handler for forwarded messages here.

    # Text handler (catches plain text messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    # Start Flask Web App mini-app server in a background thread
    if WEB_APP_ENABLE:
        try:
            default_chat_id = MINI_APP_DEFAULT_CHAT_ID or next(iter(ALLOWED_USER_IDS), None)
            flask_app = create_web_app(
                str(DOWNLOAD_DIR),
                BOT_TOKEN,
                download_jobs=download_jobs,
                zip_jobs=mini_app_zip_jobs,
                bot_loop=asyncio.get_event_loop(),
                bot_app=app,
                start_download=start_download_from_source,
                pause_download=pause_job,
                resume_download=resume_job,
                cancel_download=cancel_job,
                upload_selected=upload_mini_app_selection,
                zip_selected=zip_upload_mini_app_selection,
                default_chat_id=default_chat_id,
            )
            
            def run_flask():
                # Run Flask in a separate thread with HTTPS
                import os as os_ssl
                cert_path = os_ssl.path.join(BASE_DIR, 'cert.pem')
                key_path = os_ssl.path.join(BASE_DIR, 'key.pem')
                
                # Check if certificates exist
                if os_ssl.path.exists(cert_path) and os_ssl.path.exists(key_path):
                    ssl_context = (cert_path, key_path)
                else:
                    ssl_context = None
                    logger.warning("SSL certificates not found. Flask running without HTTPS")
                
                flask_app.run(
                    host=WEB_APP_HOST,
                    port=WEB_APP_PORT,
                    debug=False,
                    use_reloader=False,
                    threaded=True,
                    ssl_context=ssl_context
                )
            
            flask_thread = threading.Thread(target=run_flask, daemon=True)
            flask_thread.start()
            
            logger.info(
                "Flask Web App started at %s (Mini-App URL: %s)",
                f"{WEB_APP_HOST}:{WEB_APP_PORT}",
                WEB_APP_URL
            )
        except Exception as exc:
            logger.warning("Unable to start Flask Web App: %s", exc)

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
