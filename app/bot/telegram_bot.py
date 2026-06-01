# -*- coding: utf-8 -*-

import os
import re
import math
import time
import shutil
import subprocess
import asyncio
import mimetypes
import uuid
import hashlib
import logging
import tempfile
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from urllib.parse import unquote
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import sys

# Load environment variables from .env file before any config is read.
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

from app.bot.dashboard import start_dashboard_server, stop_dashboard_server

load_dotenv()

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest

from pyrogram import Client
from pyrogram.errors import RPCError, FloodWait
import yt_dlp

# Import thumbnail generation module
from app.services.thumbnails import generate_contact_sheet

# Import zipping utilities module
from app.services.archive import (
    ZipProgress,
    make_archive_with_progress,
    render_progress_bar,
    ZIP_LOCKS,
    MAX_ZIP_PART_SIZE,
    filter_files_for_archiving,
    get_oversized_file_warnings,
    check_password_support,
    check_archive_format_support,
    human_size as zip_human_size,
    sanitize_filename as zip_sanitize,
)

# Import zip settings module
from app.services.user_settings import (
    get_user_settings,
    save_user_settings,
    update_setting,
    get_setting,
    format_settings_text,
    validate_password,
    validate_part_size,
    validate_compression_level,
)

# Import post downloader module for handling forwarded posts
from app.handlers.forwarded_media import setup_pyrogram_forwarded_downloads

# Import TPB crawler subsystem
from app.downloaders.torrents.tpb import TPBCrawler, TPBHandlers
from app.downloaders.torrents.tpb.keyboards import tpb_categories_keyboard

from telegram.error import TimedOut, NetworkError, BadRequest

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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "").strip()

PYRO_SESSION_NAME = os.getenv("PYRO_SESSION_NAME", "pyrogram_uploader")
ARIA2_BIN = os.getenv("ARIA2_BIN", "aria2c")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")

# TPB crawler config
TPB_API_URL = os.getenv("TPB_API_URL", "").strip()

FILES_PER_PAGE = 8
MAX_SEND_SIZE = 2 * 1024 * 1024 * 1024  # 2 GB

pyro_client = None
upload_jobs = {}  # {job_id: {"status": "pending|uploading|completed|failed", "files": [...], ...}}
upload_queue = []  # Queue of upload job IDs
upload_counter = 0
upload_lock = asyncio.Lock()

download_jobs = {}
job_counter = 0
jobs_lock = asyncio.Lock()

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
dashboard_messages = {}  # {chat_id: {"message_id": int, "last_update": float}}

# Pinned live dashboard tracking
live_dashboard_tasks = {}  # {chat_id: asyncio.Task}
pinned_dashboard_messages = {}  # {chat_id: message_id}

# Pending yt-dlp conversion selections
pending_ytdlp_requests = {}  # {user_id: {"url": str, "chat_id": int}}

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
AUTO_CLEANUP_DAYS = int(os.getenv("AUTO_CLEANUP_DAYS", "7") or "7")

# Web dashboard configuration
WEB_DASHBOARD_ENABLE = os.getenv("WEB_DASHBOARD_ENABLE", "false").strip().lower() in {"1", "true", "yes", "on"}
WEB_DASHBOARD_HOST = os.getenv("WEB_DASHBOARD_HOST", "127.0.0.1").strip()
WEB_DASHBOARD_PORT = int(os.getenv("WEB_DASHBOARD_PORT", "8080") or "8080")

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
        "magnet_help": "🧲 Send a magnet link to start downloading.",
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
        "unknown_input": "Unknown input.\nUse the keyboard below or send a magnet link.",
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
        "magnet_help": "🧲 یک لینک مغناطیسی برای شروع دانلود ارسال کنید.",
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
        "unknown_input": "ورودی نامعلوم.\nاز صفحه کلید زیر استفاده کنید یا یک لینک مغناطیسی ارسال کنید.",
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
        return unquote(m.group(1)).strip() or "Unknown torrent"
    except Exception:
        return "Unknown torrent"


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
    try:
        if chat_id in status_messages:
            msg_data = status_messages[chat_id]
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=msg_data["message_id"],
                    text=build_status_text(u),
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(f"{ICON_REFRESH} {clean_emoji_prefix(get_lang(u, 'refresh'))}", 
                                               callback_data="refresh_status"),
                            InlineKeyboardButton(f"{ICON_HOME} {clean_emoji_prefix(get_lang(u, 'home_btn'))}", 
                                               callback_data="menu_home")
                        ]
                    ]),
                    disable_web_page_preview=True,
                )
                msg_data["last_update"] = time.time()
                return
            except Exception:
                # Message not found or expired, remove it
                del status_messages[chat_id]
        
        # Send new status message
        msg = await app.bot.send_message(
            chat_id=chat_id,
            text=build_status_text(u),
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"{ICON_REFRESH} {clean_emoji_prefix(get_lang(u, 'refresh'))}", 
                                       callback_data="refresh_status"),
                    InlineKeyboardButton(f"{ICON_HOME} {clean_emoji_prefix(get_lang(u, 'home_btn'))}", 
                                       callback_data="menu_home")
                ]
            ]),
            disable_web_page_preview=True,
        )
        status_messages[chat_id] = {
            "message_id": msg.message_id,
            "last_update": time.time(),
        }
    except Exception:
        pass


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
                            InlineKeyboardButton(f"{ICON_HOME} {clean_emoji_prefix(get_lang(u, 'home_btn'))}", 
                                               callback_data="menu_home")
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
                    InlineKeyboardButton(f"{ICON_HOME} {clean_emoji_prefix(get_lang(u, 'home_btn'))}", 
                                       callback_data="menu_home")
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
    return ReplyKeyboardMarkup(
        [
            # Primary Navigation
            [get_lang(u, 'home'), get_lang(u, 'status')],
            
            # Download Management
            [get_lang(u, 'queue'), f"{ICON_STOP} {clean_emoji_prefix(get_lang(u, 'cancel'))}"],
            
            # File Management
            [f"{ICON_FOLDER} File Browser", f"{ICON_BROOM} {clean_emoji_prefix(get_lang(u, 'clear'))}"],
            
            # Utilities
            [clean_emoji_prefix(get_lang(u, 'zip_menu')), get_lang(u, 'settings'), get_lang(u, 'help')],
            
            # Search & Settings
            [get_lang(u, 'tpb_search')],
            [get_lang(u, 'toggle_language')],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=get_lang(u, 'magnet_help'),
    )


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
        f"{ICON_QUEUE} {clean_emoji_prefix(get_lang(u, 'queue_help'))}\n"
        f"{ICON_STOP} {clean_emoji_prefix(get_lang(u, 'cancel_help'))}\n"
        f"- {get_lang(u, 'cancel_help2')}\n"
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
        j for j in download_jobs.values()
        if j["status"] in ("starting", "downloading", "metadata", "allocating")
    ]

    if not active:
        return (
            f"{ICON_STATUS} {clean_emoji_prefix(get_lang(u, 'status'))}\n\n"
            f"{get_lang(u, 'no_active')}\n\n"
            f"{ICON_FOLDER} {clean_emoji_prefix(get_lang(u, 'folder'))}\n{DOWNLOAD_DIR}\n\n"
            f"{ICON_UPLOAD} {clean_emoji_prefix(get_lang(u, 'target'))}\n{get_lang(u, 'target_val')}"
        )

    lines = [
        f"{ICON_STATUS} {clean_emoji_prefix(get_lang(u, 'status'))}",
        "",
        f"{get_lang(u, 'active_jobs')} {len(active)}",
        f"{ICON_UPLOAD} {clean_emoji_prefix(get_lang(u, 'target'))}: {get_lang(u, 'target_val')}",
        ""
    ]

    for j in sorted(active, key=lambda x: x["id"]):
        lines.extend([
            f"{ICON_DOWNLOAD} {get_lang(u, 'job_number').format(j['id'])}",
            f"{get_lang(u, 'name')} {j['name']}",
            f"{get_lang(u, 'state')} {j['status']}",
            f"{get_lang(u, 'progress')} {j.get('progress', 0.0):.1f}%",
            f"{ICON_BOX} {human_size(j.get('completed_length', 0))} / {human_size(j.get('total_length', 0))}",
            f"{ICON_SPEED} {human_speed(j.get('download_speed', 0))}",
            f"{ICON_CLOCK} {get_lang(u, 'eta')}: {j.get('eta', 'Unknown')}",
            ""
        ])

    return "\n".join(lines).strip()


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
        InlineKeyboardButton("🏠 Home", callback_data="menu_home"),
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
        InlineKeyboardButton(f"{ICON_HOME} Home", callback_data="menu_home"),
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
    buttons.append([InlineKeyboardButton(f"{ICON_HOME} Home", callback_data="menu_home")])
    return InlineKeyboardMarkup(buttons)


def build_folder_details_markup(rel_path: str, page: int = 0):
    encoded = encode_path(rel_path)
    parent = encode_path(rel_parent(rel_path))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ICON_FOLDER} Open Folder", callback_data=f"fb:list:0:{encoded}")],
        [InlineKeyboardButton(f"{ICON_UPLOAD} Upload All Files", callback_data=f"fb:send_folder_confirm:{page}:{encoded}")],
        [InlineKeyboardButton(f"{ICON_DELETE} Delete Folder", callback_data=f"fb:delete_confirm:{page}:{encoded}")],
        [
            InlineKeyboardButton(f"{ICON_BACK} Back", callback_data=f"fb:list:{page}:{parent}"),
            InlineKeyboardButton(f"{ICON_FOLDER} Root", callback_data="fb:list:0:")
        ],
        [InlineKeyboardButton(f"{ICON_HOME} Home", callback_data="menu_home")]
    ])


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
        [InlineKeyboardButton(f"{ICON_HOME} Home", callback_data="menu_home")]
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
    if total == 0:
        pct = 0
        filled = 0
    else:
        pct = int((current / total) * 100)
        filled = int((current / total) * width)
    
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {pct}%"


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

SUPPORTED_VIDEO_SITES = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
)


def is_video_url(text: str) -> bool:
    text = text.strip().lower()

    if not text.startswith(("http://", "https://")):
        return False

    return any(site in text for site in SUPPORTED_VIDEO_SITES)


async def start_ytdlp_download(app: Application, chat_id: int, url: str, audio_only: bool = False):
    global job_counter, download_jobs

    async with jobs_lock:
        job_counter += 1
        job_id = job_counter

    job = {
        "id": job_id,
        "name": "Fetching video info...",
        "url": url,
        "chat_id": chat_id,
        "pid": None,
        "process": None,
        "status": "starting",
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "eta": "Unknown",
        "started_at": now_ts(),
        "finished_at": None,
        "last_line": "",
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

    def run_download():
        ydl_opts = {
            "outtmpl": str(DOWNLOAD_DIR / "%(title).200B [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "progress_hooks": [progress_hook],
            "concurrent_fragment_downloads": 4,
            "retries": 10,
            "fragment_retries": 10,
            "windowsfilenames": False,
        }

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

    try:
        title, filepath = await loop.run_in_executor(None, run_download)

        job["status"] = "completed"
        job["name"] = title
        job["progress"] = 100.0
        job["finished_at"] = now_ts()

        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{ICON_OK} {'MP3' if audio_only else 'Video'} download completed.\n\n"
                f"Job #{job_id}\n"
                f"Title: {title}"
            ),
            reply_markup=build_reply_menu(),
        )

    except Exception as e:
        logger.exception("yt-dlp download failed")

        job["status"] = "failed"
        job["finished_at"] = now_ts()
        job["last_line"] = str(e)

        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{ICON_FAIL} Video download failed.\n\n"
                f"Job #{job_id}\n"
                f"Reason: {e}"
            ),
            reply_markup=build_reply_menu(),
        )

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
    )


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
            [InlineKeyboardButton(f"{ICON_HOME} Home", callback_data="menu_home")],
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
            [InlineKeyboardButton(f"{ICON_HOME} Home", callback_data="menu_home")],
        ]),
    )


# =========================================================
# Aria2 direct subprocess manager
# =========================================================

def parse_aria2_line(job: dict, line: str):
    s = line.strip()
    if not s:
        return

    job["last_line"] = s
    low = s.lower()

    if "downloading metadata" in low:
        job["status"] = "metadata"
        return

    if "allocating disk space" in low:
        job["status"] = "allocating"
        return

    if "download complete" in low or "seed completed" in low or "seeding" in low:
        if job["status"] not in ("completed", "failed", "cancelled"):
            job["status"] = "downloading"

    m = re.search(
        r"SIZE:([0-9.]+)([KMGTP]?i?B)/([0-9.]+)([KMGTP]?i?B)\((\d+)%\).*?DL:([0-9.]+)([KMGTP]?i?B).*?ETA:([0-9hms]+)",
        s,
        re.I
    )

    if m:
        comp_val, comp_unit, total_val, total_unit, pct, dl_val, dl_unit, eta = m.groups()

        def unit_to_bytes(val, unit):
            unit = unit.upper()
            binary = {
                "B": 1,
                "KIB": 1024,
                "MIB": 1024**2,
                "GIB": 1024**3,
                "TIB": 1024**4,
                "PIB": 1024**5,
            }
            decimal = {
                "KB": 1000,
                "MB": 1000**2,
                "GB": 1000**3,
                "TB": 1000**4,
                "PB": 1000**5,
            }
            mult = binary.get(unit, decimal.get(unit, 1))
            return int(float(val) * mult)

        job["completed_length"] = unit_to_bytes(comp_val, comp_unit)
        job["total_length"] = unit_to_bytes(total_val, total_unit)
        job["progress"] = float(pct)
        job["download_speed"] = unit_to_bytes(dl_val, dl_unit)
        job["eta"] = eta
        job["status"] = "downloading"
        return

    m2 = re.search(r"\((\d+)%\)", s)
    if m2:
        job["progress"] = float(m2.group(1))
        if job["status"] not in ("metadata", "allocating"):
            job["status"] = "downloading"


async def monitor_aria2_output(job: dict, stream):
    while True:
        line = await stream.readline()
        if not line:
            break
        try:
            text = line.decode("utf-8", errors="ignore").rstrip()
        except Exception:
            continue
        parse_aria2_line(job, text)


async def wait_for_job_finish(app: Application, job_id: int):
    job = download_jobs[job_id]
    proc = job["process"]
    rc = await proc.wait()

    if job["status"] == "cancelled":
        return

    if rc == 0:
        job["status"] = "completed"
        logger.info(f"Download completed: {job['name']}")
        job["progress"] = 100.0
        job["finished_at"] = now_ts()

        try:
            await app.bot.send_message(
                chat_id=job["chat_id"],
                text=f"{ICON_OK} Download completed.\n\nJob #{job['id']}\nName: {job['name']}",
                reply_markup=build_reply_menu(),
            )
        except Exception:
            pass

    else:
        job["status"] = "failed"
        logger.error(f"Download failed: {job['name']}")
        job["finished_at"] = now_ts()

        try:
            await app.bot.send_message(
                chat_id=job["chat_id"],
                text=(
                    f"{ICON_FAIL} Download failed.\n\n"
                    f"Job #{job['id']}\n"
                    f"Name: {job['name']}\n"
                    f"Reason: {job.get('last_line', 'Unknown error')}"
                ),
                reply_markup=build_reply_menu(),
            )
        except Exception:
            pass


async def start_aria2_download(app: Application, chat_id: int, magnet: str):
    if shutil.which(ARIA2_BIN) is None:
        raise RuntimeError(f"aria2 executable not found: {ARIA2_BIN}")

    global job_counter, download_jobs

    async with jobs_lock:
        job_counter += 1
        job_id = job_counter

    name = extract_bt_name(magnet)

    proc = await asyncio.create_subprocess_exec(
        ARIA2_BIN,
        "--no-conf=true",
        "--enable-rpc=false",
        "--seed-time=0",
        "--dir", str(DOWNLOAD_DIR),
        "--summary-interval=2",
        "--bt-save-metadata=true",
        "--bt-metadata-only=false",
        "--follow-torrent=true",
        "--enable-color=false",
        "--console-log-level=notice",
        magnet,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    job = {
        "id": job_id,
        "name": name,
        "magnet": magnet,
        "chat_id": chat_id,
        "pid": proc.pid,
        "process": proc,
        "status": "starting",
        "progress": 0.0,
        "completed_length": 0,
        "total_length": 0,
        "download_speed": 0,
        "eta": "Unknown",
        "started_at": now_ts(),
        "finished_at": None,
        "last_line": "",
    }

    download_jobs[job_id] = job

    asyncio.create_task(monitor_aria2_output(job, proc.stdout))
    asyncio.create_task(wait_for_job_finish(app, job_id))

    return job


async def cancel_job(job_id: int):
    job = download_jobs.get(job_id)
    if not job:
        return False, f"Job #{job_id} not found."

    if job["status"] in ("completed", "failed", "cancelled"):
        return False, f"Job #{job_id} is already {job['status']}."

    proc = job.get("process")
    try:
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
    except ProcessLookupError:
        pass

    job["status"] = "cancelled"
    job["finished_at"] = now_ts()
    return True, f"Cancelled job #{job_id}: {job['name']}"


def clear_finished_jobs():
    global download_jobs

    keep = {}
    for jid, job in download_jobs.items():
        if job["status"] in ("starting", "downloading", "metadata", "allocating"):
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
        await update.message.reply_text("⛔ Unauthorized")
        return

    await update.message.reply_text(
        build_home_text(user_id), 
        reply_markup=build_reply_menu(user_id)
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text(
        build_status_text(user_id), 
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"{ICON_REFRESH} {clean_emoji_prefix(get_lang(user_id, 'refresh'))}", 
                                   callback_data="refresh_status"),
                InlineKeyboardButton(f"{ICON_HOME} {clean_emoji_prefix(get_lang(user_id, 'home_btn'))}", 
                                   callback_data="menu_home")
            ]
        ]),
        disable_web_page_preview=True,
    )
    status_messages[chat_id] = {
        "message_id": msg.message_id,
        "last_update": time.time(),
    }


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
    u = user_id or 0
    files_count = len(filter_files_for_archiving(collect_download_files()))
    
    return (
        f"{clean_emoji_prefix(get_lang(u, 'zip_menu'))}\n\n"
        f"Available files: {files_count}\n\n"
        f"Choose an option below:"
    )


def build_zip_menu_markup(user_id: int) -> InlineKeyboardMarkup:
    """Build buttons for zip menu."""
    u = user_id or 0
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'list_files'))}", callback_data="zip_menu:list")],
        [InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'select_files'))}", callback_data="zip_menu:select")],
        [InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'zip_all'))}", callback_data="zip_menu:zip_all")],
        [InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'zip_settings'))}", callback_data="zip_menu:settings")],
        [InlineKeyboardButton(f"{clean_emoji_prefix(get_lang(u, 'home_btn'))}", callback_data="menu_home")],
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

        elif normalized in ("status", "وضعیت"):
            msg = await update.message.reply_text(
                build_status_text(user_id),
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(f"{ICON_REFRESH} {clean_emoji_prefix(get_lang(user_id, 'refresh'))}", 
                                           callback_data="refresh_status"),
                        InlineKeyboardButton(f"{ICON_HOME} {clean_emoji_prefix(get_lang(user_id, 'home_btn'))}", 
                                           callback_data="menu_home")
                    ]
                ]),
                disable_web_page_preview=True,
            )
            status_messages[chat_id] = {
                "message_id": msg.message_id,
                "last_update": time.time(),
            }

        elif normalized in ("queue", "صف"):
            await update.message.reply_text(
                build_queue_text(user_id), 
                reply_markup=build_reply_menu(user_id)
            )

        elif normalized in ("files", "file browser", "مرورگر فایل"):
            await update.message.reply_text(
                build_files_text("", 0),
                reply_markup=build_files_markup("", 0),
                disable_web_page_preview=True,
            )

        elif normalized in ("cancel", "انصراف"):
            active = [
                j for j in download_jobs.values()
                if j["status"] in ("starting", "downloading", "metadata", "allocating")
            ]

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
                        InlineKeyboardButton(f"{ICON_BACK} No", callback_data="menu_home")
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
                        InlineKeyboardButton(f"{ICON_BACK} No", callback_data="menu_home")
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

        elif get_lang(user_id, 'toggle_language').lower() in lower or "language" in lower or "زبان" in text:
            lang_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(get_lang_for_all("en", "en"), callback_data="set_lang:en"),
                    InlineKeyboardButton(get_lang_for_all("fa", "fa"), callback_data="set_lang:fa"),
                ],
                [InlineKeyboardButton(f"{ICON_HOME} {get_lang(user_id, 'home_btn')}", callback_data="menu_home")]
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

            job = await start_aria2_download(context.application, chat_id, text)

            await update.message.reply_text(
                (
                    f"{ICON_MAGNET} {get_lang(user_id, 'magnet_received')}\n\n"
                    f"{get_lang(user_id, 'started')} {job['id']}\n"
                    f"{get_lang(user_id, 'name')} {name}\n"
                    f"{get_lang(user_id, 'pid')}: {job['pid']}"
                ),
                reply_markup=build_reply_menu(user_id),
                disable_web_page_preview=True,
            )

        elif is_video_url(text):
            pending_ytdlp_requests[user_id] = {
                "url": text,
                "chat_id": chat_id,
            }

            await update.message.reply_text(
                f"{ICON_DOWNLOAD} Choose download format:",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("🎬 Video", callback_data="ytdlp_video"),
                        InlineKeyboardButton("🎵 MP3", callback_data="ytdlp_mp3"),
                    ],
                    [InlineKeyboardButton(f"{ICON_HOME} Home", callback_data="menu_home")]
                ]),
            )

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
                            callback_data="menu_home",
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

    files = get_torrent_file_list(torrent_path)

    if not files:
        await update.message.reply_text(f"{ICON_FAIL} Could not read torrent file list.")
        return

    user_id = update.effective_user.id
    torrent_select_sessions[user_id] = {
        "torrent_path": str(torrent_path),
        "files": files,
        "selected": set(),
        "page": 0,
    }

    await update.message.reply_text(
        f"{ICON_MAGNET} Select files to download:",
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
                    InlineKeyboardMarkup([[InlineKeyboardButton(f"{ICON_HOME} {get_lang(user_id, 'home_btn')}", callback_data="menu_home")]])
                )
            else:
                await query.answer(msg, show_alert=True)
        
        elif data == "clear_confirm":
            removed = clear_finished_jobs()
            await safe_edit_message(
                query.message,
                f"{ICON_OK} {get_lang(user_id, 'cleared')}: {removed} {get_lang(user_id, 'job_id')}(s)",
                InlineKeyboardMarkup([[InlineKeyboardButton(f"{ICON_HOME} {get_lang(user_id, 'home_btn')}", callback_data="menu_home")]])
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
                session["torrent_path"] + f" --select-file={','.join(selected)}"
            )

            await query.edit_message_text(f"{ICON_SPEED} {get_lang(user_id, 'preparing')}...")

        elif data == "tall":
            session = torrent_select_sessions.pop(user_id)

            await start_aria2_download(
                context.application,
                chat_id,
                session["torrent_path"]
            )

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
                        [InlineKeyboardButton(f"{ICON_HOME} Home", callback_data="menu_home")],
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
                        [InlineKeyboardButton(f"{ICON_HOME} Home", callback_data="menu_home")]
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
                                callback_data="menu_home",
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
                        [InlineKeyboardButton(get_lang(user_id, "home_btn"), callback_data="menu_home")],
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
                        [InlineKeyboardButton(get_lang(user_id, "home_btn"), callback_data="menu_home")],
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
                [InlineKeyboardButton(f"{ICON_HOME} Home", callback_data="menu_home")],
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
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("forwardedposts", forwarded_posts_cmd))
    app.add_handler(CommandHandler("autoforward", forwarded_posts_cmd))

    # Zipping feature handlers
    app.add_handler(CommandHandler("zip", zip_files_cmd))
    app.add_handler(CommandHandler("list", list_files_cmd))
    app.add_handler(CommandHandler("clear", clear_files_cmd))

    # TPB crawler handlers
    tpb_crawler = TPBCrawler(TPB_API_URL)

    async def tpb_start_download(update, context, magnet: str):
        """Wrapper to start aria2 download from TPB with duplicate check."""
        name = extract_bt_name(magnet)
        if is_duplicate_name(name):
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ Duplicate detected: {name}",
            )
            return {"id": 0, "name": name, "status": "duplicate"}
        job = await start_aria2_download(context.application, update.effective_chat.id, magnet)
        return job

    tpb_handlers = TPBHandlers(
        tpb_crawler,
        lang_func=get_lang,
        download_func=tpb_start_download,
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
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.FileExtension("torrent"), on_torrent_file))

    # Forwarded media is handled natively by Pyrogram (see _wrapped_post_init)
    # DO NOT register a PTB handler for forwarded messages here.

    # Text handler (catches plain text messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
