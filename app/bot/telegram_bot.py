# -*- coding: utf-8 -*-

import asyncio
import hashlib
import html
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
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

# Pyrogram 2.x calls asyncio.get_event_loop() while building its sync wrappers
# at import time. From Python 3.12 that is deprecated and on 3.14 it raises
# outright ("There is no current event loop"), so importing the bot fails before
# any of our code runs. Make a loop current first; telegram_bot.main() installs
# the loop it actually runs on later.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from pyrogram import Client
from pyrogram import StopTransmission
from pyrogram.errors import FloodWait, RPCError
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from app.downloaders.base import DownloadRequest
from app.downloaders.spotify import SpotifyDownloader, is_spotify_url

from app.bot.callbacks import answer_once, auto_answer
from app.bot.search import SearchUI
from app.bot.views import files as file_views
from app.bot.views import home as home_views
from app.bot.views import jobs as job_views
from app.bot.views import settings as settings_views
from app.bot.views.common import Screen
from app.downloaders.torrents.prowlarr.client import ProwlarrClient
from app.downloaders.torrents.rarbg.crawler import RARBGCrawler
from app.downloaders.torrents.tpb.crawler import TPBCrawler

# Import post downloader module for handling forwarded posts
from app.handlers.forwarded_media import bot_id_from_token, setup_pyrogram_forwarded_downloads
from app.infrastructure.aria2_rpc import Aria2DaemonConfig, Aria2RpcClient, Aria2RpcError
from app.services.adult_video_resolver import (
    resolve_adult_video_url,
    resolved_video_output_template,
)

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
from app.services.batch_download import (
    BatchDownloadMode,
    BatchProgress,
    batch_download_mode_description,
    batch_download_mode_label,
    normalize_batch_download_mode,
    run_sequential_batch,
)
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
from app.services.gallery import download_gallery, gallery_category, site_label
from app.services import animation
from app.services import cookies as cookie_store
from app.services.cobalt import CobaltClient, CobaltError
from app.services.job_store import JobStore
from app.services.organize import OrganizePlan, apply_organize, plan_organize
from app.services.pornhub_model import (
    PornHubModelPlaylist,
    is_pornhub_model_url,
    resolve_pornhub_model_playlist,
)
from app.services.runtime_dependencies import configure_deno_runtime, get_deno_version

# Import thumbnail generation module
from app.services.thumbnails import generate_contact_sheet
from app.services.torrent_search import ProwlarrProvider, RARBGProvider, TPBProvider

# Import zip settings module
from app.services.user_settings import (
    get_user_settings,
    update_setting,
    validate_compression_level,
    validate_part_size,
    validate_password,
)
from app.services.video_sites import (
    is_adult_video_url,
    is_hentai_video_url,
    is_supported_video_url,
    requires_deno_runtime,
    requires_ytdlp_generic_impersonation,
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
GALLERY_DIR = BASE_DIR / "Download" / "Gallery"


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
# Optional: a Pyrogram/Kurigram session string, for hosts where an interactive
# login (phone number + code) is not possible. Treat it like a password.
PYRO_SESSION_STRING = os.getenv("PYRO_SESSION_STRING", "").strip()
ARIA2_BIN = os.getenv("ARIA2_BIN", "aria2c")
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
DENO_BIN = os.getenv("DENO_BIN", "").strip()
SPOTDL_BIN = os.getenv("SPOTDL_BIN", "spotdl")
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "").strip()
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip()
# cookies.txt sent to the bot (Settings → Cookies); used before YTDLP_COOKIES_FILE.
UPLOADED_COOKIES_PATH = BASE_DIR / "data" / "cookies.txt"
# Your own cobalt instance (https://github.com/imputnet/cobalt), tried when yt-dlp fails.
COBALT_API_URL = os.getenv("COBALT_API_URL", "").strip()
COBALT_API_KEY = os.getenv("COBALT_API_KEY", "").strip()
ARIA2_RPC_HOST = os.getenv("ARIA2_RPC_HOST", "127.0.0.1").strip() or "127.0.0.1"
ARIA2_RPC_PORT = parse_env_int("ARIA2_RPC_PORT", 6800)
ARIA2_RPC_SECRET = os.getenv("ARIA2_RPC_SECRET", "").strip()
SUPPORTED_SITES_URL = (
    os.getenv(
        "SUPPORTED_SITES_URL",
        "https://github.com/cyanologyst/Telegram-Aio-Downloader/blob/main/docs/SUPPORTED_SITES.md",
    ).strip()
    or "https://github.com/cyanologyst/Telegram-Aio-Downloader/blob/main/docs/SUPPORTED_SITES.md"
)

# TPB crawler config
TPB_API_URL = os.getenv("TPB_API_URL", "").strip()
RARBG_BASE_URL = os.getenv("RARBG_BASE_URL", "").strip()
PROWLARR_URL = os.getenv("PROWLARR_URL", "http://127.0.0.1:9696").strip()
PROWLARR_API_KEY = os.getenv("PROWLARR_API_KEY", "").strip()
try:
    PROWLARR_SEARCH_LIMIT = int(os.getenv("PROWLARR_SEARCH_LIMIT", "20") or "20")
except ValueError:
    PROWLARR_SEARCH_LIMIT = 20

DENO_PATH = configure_deno_runtime(DENO_BIN)
DENO_VERSION = get_deno_version(DENO_PATH)

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
# Jobs are saved to SQLite so the list, and running aria2 downloads, survive restarts.
JOB_DB_PATH = Path(os.getenv("JOB_DB_PATH", "").strip() or BASE_DIR / "data" / "bot.sqlite3")
job_store: "JobStore | None" = None
JOB_SAVE_INTERVAL = 10
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
PATH_TOKEN_TIMEOUT = 24 * 3600  # menus stay usable for a day

# Unified torrent search UI, built in main()
search_ui: SearchUI | None = None

# Torrent file selection sessions
torrent_select_sessions = {}

# Batch file selection sessions for multi-select upload
batch_select_sessions = {}  # {user_id: {"rel_path", "selected", "page", "mode": "upload"|"delete"}}

# Zip file selection sessions
zip_select_sessions = {}  # {user_id: {"selected": set(), "page": int}}

# Pending zip name sessions (waiting for user to provide zip file name)
pending_zip_name_sessions = {}  # {user_id: {"mode": "all"|"selected", "session": {...}}}

DEFAULT_LANGUAGE = "en"

# Status refresh tracking - one dashboard per chat
status_messages = {}  # {chat_id: {"message_id": int, "last_update": float, "text_hash": str}}
status_message_locks = {}  # {chat_id: asyncio.Lock}
# How often the live status dashboard may be edited, in seconds. Edits are
# rate limited by Telegram; values below ~5s risk RetryAfter storms and the
# "70+ messages per minute" spam caused by edit failures recreating messages.
STATUS_AUTO_UPDATE_SECONDS = parse_env_int("STATUS_UPDATE_INTERVAL", 8)

# Link prompts waiting for a button press ("Video or MP3?", "Download all?",
# "Download anyway?"). Keyed by a request id carried in the buttons, so every
# prompt acts on its own link even when several are open.
link_requests: dict[str, dict] = {}
LINK_REQUEST_TTL = 24 * 3600


def store_link_request(kind: str, chat_id: int, **payload) -> str:
    now = time.time()
    for rid in [r for r, req in link_requests.items() if now - req["created"] > LINK_REQUEST_TTL]:
        link_requests.pop(rid, None)
    rid = uuid.uuid4().hex[:10]
    link_requests[rid] = {"kind": kind, "chat_id": chat_id, "created": now, **payload}
    return rid


def link_request_markup(rid: str, actions: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Buttons for a link prompt: the given (label, action) pairs plus Cancel."""
    buttons = [InlineKeyboardButton(label, callback_data=f"lp:{rid}:{action}") for label, action in actions]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data=f"lp:{rid}:cancel")])
    return InlineKeyboardMarkup(rows)


# Dedicated executors for CPU-intensive operations
# Using ProcessPoolExecutor for py7zr since it's CPU-bound
# Limiting to 2 workers to prevent bot overload
zip_executor = None  # Will be initialized in post_init


# Logging
LOG_DIR = Path(os.getenv("LOG_DIR", "").strip() or BASE_DIR / "logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

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
        "select_files_zip": "☑️ Select Files to Zip",
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
        "rarbg_search": "🧲 RARBG Search",
        "rarbg_welcome": "RARBG-style Search",
        "rarbg_send_query": "Send a search query to find torrents on the configured RARBG-style mirror.",
        "rarbg_fetching": "Fetching torrent details...",
        "rarbg_download_started": "Download started!",
        "rarbg_paste_to_download": "Paste this magnet link to start downloading.",
        "rarbg_tap_download": "Tap 📥 to download instantly",
        "prowlarr_search": "🧭 Prowlarr Search",
        "prowlarr_welcome": "Prowlarr Search",
        "prowlarr_send_query": "Send a query to search all configured Prowlarr indexers.",
        "prowlarr_not_configured": "Prowlarr is not configured yet. Set PROWLARR_URL and PROWLARR_API_KEY in .env.",
        "prowlarr_download_started": "Download started!",
        "prowlarr_tap_download": "Tap 📥 for all files or ☑️ to select torrent files.",
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
    }
}


def get_lang(user_id: int, key: str) -> str:
    """UI string by key. The bot is English-only; user_id is kept for call sites."""
    return LANGUAGES[DEFAULT_LANGUAGE].get(key, key)


def is_authorized_user(user_id: int) -> bool:
    # main() refuses to start with an empty allow-list, so an empty set here
    # means "nobody", never "everybody".
    return user_id in ALLOWED_USER_IDS


async def authorization_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop every update that does not come from an allowed user.

    Registered in handler group -1 so it runs before any command, message or
    button handler. Unauthorized users are ignored silently: replying would
    only confirm the bot exists.
    """
    user = getattr(update, "effective_user", None)
    if user is not None and is_authorized_user(user.id):
        return
    if user is not None:
        logger.warning("Ignored update from unauthorized user %s", user.id)
    raise ApplicationHandlerStop


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


def default_archive_name() -> str:
    return f"archive_{datetime.now().strftime('%Y%m%d_%H%M')}"


def sanitize_archive_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip("._- ")[:100]


async def ask_zip_name(message, user_id: int, chat_id: int, paths: list, *, default_name: str, cancel_data: str):
    """Ask for an archive name (or offer a default) before zipping ``paths``."""
    paths = sorted(paths, key=lambda path: str(path).lower())
    pending_zip_name_sessions[user_id] = {
        "files_to_zip": build_files_to_zip(paths),
        "source_files": paths,
        "settings": get_user_settings(user_id),
        "default_name": sanitize_archive_name(default_name) or default_archive_name(),
        "chat_id": chat_id,
    }
    size = sum(path.stat().st_size for path in paths)
    await show_screen(message, file_views.zip_name_screen(
        len(paths), size, pending_zip_name_sessions[user_id]["default_name"], cancel_data
    ))


async def run_named_zip(app: Application, user_id: int, name: str, status_msg) -> None:
    """Create the archive for a pending zip session and send it to the chat."""
    session = pending_zip_name_sessions.pop(user_id, None)
    if session is None:
        await status_msg.edit_text("This archive request has expired. Start it again from Archive.")
        return
    chat_id = session["chat_id"]
    settings = session["settings"]
    await status_msg.edit_text(f"📦 Creating {name}...")

    async def on_progress(prog_text: str):
        try:
            await status_msg.edit_text(prog_text)
        except BadRequest:
            pass

    sent_while_zipping = []

    upload_callback = create_zip_upload_callback(app, chat_id, user_id, settings, status_msg, sent_while_zipping)
    zip_paths, size_warnings = await run_archive_job(
        user_id,
        session["files_to_zip"],
        DOWNLOAD_DIR,
        zip_name=name,
        settings=settings,
        on_progress=on_progress,
        upload_callback=upload_callback,
    )
    all_ok = True
    if zip_paths:
        all_ok = await send_archives_to_chat(app, chat_id, zip_paths, settings, status_msg, user_id)

    deleted_sources = False
    if all_ok and settings.get("auto_delete_files_after_zip"):
        for path in session["source_files"]:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not delete zipped file %s: %s", path, exc)
        deleted_sources = True

    parts = len(sent_while_zipping) + len(zip_paths)
    lines = [
        f"{ICON_OK if all_ok else ICON_WARN} Archive {'sent' if all_ok else 'partly sent'}: {name}",
        f"Files: {len(session['source_files'])} · parts: {parts}",
    ]
    if sent_while_zipping:
        lines.append("Parts were uploaded as they were created and removed from disk.")
    elif zip_paths and settings.get("auto_delete_zips_after_send") and all_ok:
        lines.append("The archive was removed from disk after sending.")
    elif zip_paths:
        lines.append("The archive is kept in the Download folder.")
    if deleted_sources:
        lines.append("The original files were deleted (Archive settings).")
    if not all_ok:
        lines.append("Some parts failed to send; check the bot log.")
    lines += [f"{ICON_WARN} {warning}" for warning in size_warnings]
    await status_msg.edit_text("\n".join(lines))


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
    context, chat_id: int, user_id: int, settings: dict, status_msg=None, sent: list | None = None
):
    """Upload each archive part as soon as it is written, then delete it.

    The archive is built in a worker thread; this returns a plain function for
    that thread which schedules the upload on the bot's event loop and waits
    for it. The loop is captured here, while running on it: the worker thread
    has no loop of its own, so asking for one there failed and every part
    piled up on disk until the end.
    """
    loop = asyncio.get_running_loop()

    async def upload_and_delete(part_path: Path, current_vol: int, total_vols: int):
        size = part_path.stat().st_size
        caption = f"📦 Part {current_vol}/{total_vols}: {part_path.name}\nSize: {zip_human_size(size)}"
        if total_vols > 1 and current_vol == 1:
            caption += "\nOpen the .001 file with 7-Zip or WinRAR to extract everything."
        if status_msg is not None:
            try:
                await status_msg.edit_text(
                    f"📤 Sending part {current_vol}/{total_vols}: {part_path.name}\n"
                    "Each part is removed from disk once it has been sent."
                )
            except Exception as exc:
                logger.debug("Progress update failed: %s", exc)
        if not await send_archive_document(context, chat_id, part_path, caption):
            return False, f"Failed to send part {current_vol}"
        part_path.unlink(missing_ok=True)
        if sent is not None:
            sent.append(part_path.name)
        return True, None

    def sync_upload_callback(part_path: Path, current_vol: int, total_vols: int) -> tuple:
        future = asyncio.run_coroutine_threadsafe(
            upload_and_delete(part_path, current_vol, total_vols), loop
        )
        try:
            # Big parts take a while; never give up on one that is still sending.
            return future.result()
        except Exception as exc:
            logger.error("Archive part upload failed: %s", exc)
            return False, str(exc)

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
        # With an upload callback every part may already be sent and deleted.
        if not paths and upload_callback is None:
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


class _DashboardMessageGone(Exception):
    """Raised when a tracked status/dashboard message must be recreated."""


async def _edit_dashboard_message(
    app: Application,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup,
    parse_mode: str | None = None,
) -> bool:
    """Edit a dashboard/status message, classifying failures correctly.

    Returns True when the message now shows `text` (edited or already
    identical). Raises _DashboardMessageGone only when the target message no
    longer exists or can never be edited, so callers may safely recreate it.

    This is the fix for progress-message spam: transient errors (rate limits,
    network hiccups) must NOT cause callers to send a brand-new message.
    """
    try:
        await app.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
        return True
    except BadRequest as exc:
        lowered = str(exc).lower()
        if "message is not modified" in lowered:
            return True
        if any(
            term in lowered
            for term in (
                "message to edit not found",
                "message can't be edited",
                "there is no text in the message to edit",
                "chat not found",
                "message id is invalid",
            )
        ):
            raise _DashboardMessageGone() from exc
        logger.warning("Status edit rejected (%s); skipping this update", exc)
        return False
    except RetryAfter as exc:
        retry_after = min(float(getattr(exc, "retry_after", 5) or 5), 15.0)
        logger.warning("Telegram rate limit on status edits; backing off %.1fs", retry_after)
        await asyncio.sleep(retry_after)
        return False
    except (TimedOut, NetworkError) as exc:
        logger.warning("Network error editing status message: %s", exc)
        return False


def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def build_status_screen():
    return job_views.status_screen(list(download_jobs.values()), free_disk_bytes())


async def update_status_message(app: Application, chat_id: int, user_id: int = None):
    """Refresh the chat's status dashboard, if one is showing. Never sends a new one."""
    lock = status_message_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        msg_data = status_messages.get(chat_id)
        # Re-check inside the lock: concurrent monitor loops pass a pre-lock check together.
        if not msg_data or time.time() - msg_data.get("last_update", 0) < 2.0:
            return
        text, markup = build_status_screen()
        text_hash = _hash_content(text)
        if msg_data.get("text_hash") == text_hash:
            # Nothing visible changed; skipping the API call kills most edit traffic.
            msg_data["last_update"] = time.time()
            return
        try:
            if await _edit_dashboard_message(
                app, chat_id, msg_data["message_id"], text, markup, parse_mode=ParseMode.HTML
            ):
                msg_data.update(last_update=time.time(), text_hash=text_hash)
            # On a transient failure keep the same message; the next tick retries.
            # Recreating here is what used to flood the chat with duplicates.
        except _DashboardMessageGone:
            status_messages.pop(chat_id, None)


async def show_status_dashboard(app: Application, chat_id: int, user_id: int = None, replace=None):
    """Show the status dashboard where the user is looking.

    From the keyboard or /status it is sent fresh at the bottom of the chat and
    the previous dashboard is deleted, so pressing Status always shows
    something. From an inline menu (``replace``) that message becomes the
    dashboard. Either way it is the one message auto-updates edit from then on.
    """
    text, markup = build_status_screen()
    lock = status_message_locks.setdefault(chat_id, asyncio.Lock())
    async with lock:
        previous = status_messages.pop(chat_id, None)
        message_id = None
        if replace is not None:
            try:
                await replace.edit_text(
                    text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )
                message_id = replace.message_id
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    message_id = replace.message_id
        if message_id is None:
            msg = await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            message_id = msg.message_id
        if previous and previous.get("message_id") != message_id:
            try:
                await app.bot.delete_message(chat_id=chat_id, message_id=previous["message_id"])
            except Exception as exc:
                logger.debug("Old status message not deleted: %s", exc)
        status_messages[chat_id] = {
            "message_id": message_id,
            "last_update": time.time(),
            "user_id": user_id or 0,
            "text_hash": _hash_content(text),
        }


async def maybe_auto_update_status_message(app: Application, job: dict, force: bool = False):
    """Progress hook used by every downloader: refresh the job's card and the dashboard."""
    if not job.get("status_visible", True):
        return
    # A finished job gets a fresh final card from finish_job_card(); editing
    # the progress card first would be a wasted API call.
    if job_views.is_active(job):
        await refresh_job_card(app, job, force=force)

    msg_data = status_messages.get(job["chat_id"])
    if not msg_data:
        return
    if not force and time.time() - msg_data.get("last_update", 0) < STATUS_AUTO_UPDATE_SECONDS:
        return
    await update_status_message(app, job["chat_id"], job.get("user_id") or msg_data.get("user_id") or 0)


# =========================================================
# Job cards: one live message per download
# =========================================================

# Telegram allows roughly one edit per second per chat. Cards share that
# budget, so the more jobs are running, the less often each card refreshes.
CARD_MIN_INTERVAL = 4.0


def _abs_download_path(path) -> Path | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = DOWNLOAD_DIR / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(DOWNLOAD_DIR.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.exists() else None


def job_outputs(job: dict) -> list[str]:
    """Files/folders a finished job produced, relative to Download/, that still exist."""
    candidates = list(job.get("outputs") or [])
    if job.get("filepath"):
        candidates.append(job["filepath"])
    if job.get("pdf_path"):
        candidates.append(job["pdf_path"])
    elif job.get("provider") == "manga" and job.get("folder") and job.get("status") == "completed":
        candidates.append(job["folder"])
    candidates += list(job.get("files") or [])  # aria2: the selected files

    rels: list[str] = []
    root = DOWNLOAD_DIR.resolve()
    for candidate in candidates:
        resolved = _abs_download_path(candidate)
        if resolved is None or resolved == root:
            continue
        rel = str(resolved.relative_to(root)).replace("\\", "/")
        if rel not in rels:
            rels.append(rel)
    return rels


def job_open_data(outputs: list[str]) -> str | None:
    """Callback data for the card's Open button: the file, or the folder holding the outputs."""
    if not outputs:
        return None
    if len(outputs) == 1:
        full = DOWNLOAD_DIR / outputs[0]
        if full.is_file():
            return f"fb:file:0:{encode_path(outputs[0])}"
        return f"fb:list:0:{encode_path(outputs[0])}"
    common = os.path.commonpath(outputs) if len(outputs) > 1 else outputs[0]
    common = "" if common in (".", "/") else common
    return f"fb:list:0:{encode_path(common)}"


def render_job_card(job: dict, confirm: str | None = None):
    outputs = job_outputs(job) if job.get("status") == "completed" else []
    return job_views.job_card(
        job, open_data=job_open_data(outputs), can_upload=bool(outputs), confirm=confirm
    )


async def _send_card(app: Application, job: dict):
    text, markup = render_job_card(job)
    msg = await app.bot.send_message(
        chat_id=job["chat_id"],
        text=text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    job["card"] = {
        "chat_id": job["chat_id"],
        "message_id": msg.message_id,
        "hash": _hash_content(text + repr(markup)),
        "last": time.time(),
    }


async def attach_job_card(app: Application, job: dict, message=None):
    """Give a job its card: turn ``message`` (e.g. the link prompt) into it, or send one."""
    if job.get("card_final"):
        # Finished before we got here; its final card is already in the chat.
        if message is not None:
            try:
                await message.delete()
            except Exception:
                pass
        return
    if message is not None:
        text, markup = render_job_card(job)
        try:
            await message.edit_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
            job["card"] = {
                "chat_id": message.chat_id,
                "message_id": message.message_id,
                "hash": _hash_content(text + repr(markup)),
                "last": time.time(),
            }
            return
        except BadRequest as exc:
            logger.debug("Could not turn the prompt into a job card: %s", exc)
    try:
        await _send_card(app, job)
    except Exception as exc:
        logger.warning("Could not send job card for #%s: %s", job.get("id"), exc)


async def refresh_job_card(app: Application, job: dict, force: bool = False, confirm: str | None = None):
    card = job.get("card")
    if not card or job.get("card_final"):
        return
    active_cards = sum(
        1 for j in download_jobs.values() if j.get("card") and job_views.is_active(j)
    )
    interval = max(CARD_MIN_INTERVAL, 1.5 * active_cards)
    if not force and time.time() - card.get("last", 0) < interval:
        return
    text, markup = render_job_card(job, confirm=confirm)
    content_hash = _hash_content(text + repr(markup))
    if content_hash == card.get("hash"):
        card["last"] = time.time()
        return
    try:
        if await _edit_dashboard_message(
            app, card["chat_id"], card["message_id"], text, markup, parse_mode=ParseMode.HTML
        ):
            card.update(hash=content_hash, last=time.time())
    except _DashboardMessageGone:
        job.pop("card", None)
        await _send_card(app, job)


GIFS_PER_JOB = 10
GIF_PROVIDERS = {"yt-dlp", "gallery-dl"}
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "").strip() or str(
    Path(FFMPEG_BIN).with_name("ffprobe") if os.sep in FFMPEG_BIN else "ffprobe"
)


def gif_candidates(job: dict) -> list[Path]:
    files: list[Path] = []
    for rel in job_outputs(job):
        full = DOWNLOAD_DIR / rel
        if full.is_dir():
            files += sorted(p for p in full.rglob("*") if p.is_file())
        elif full.is_file():
            files.append(full)
    return [f for f in files if f.suffix.lower() in animation.VIDEO_SUFFIXES]


def prepare_gifs(job: dict, workdir: Path) -> tuple[list[tuple[Path, object]], int, bool]:
    """Pick the job's GIF-like outputs and make them Telegram-ready.

    Returns (files with their media info, how many were too big, whether a clip was cut).
    A GIF job (``job["gif"]``) treats every video as a GIF.
    """
    forced = bool(job.get("gif"))
    ready: list[tuple[Path, object]] = []
    too_big = 0
    trimmed = False
    for path in gif_candidates(job):
        if len(ready) >= GIFS_PER_JOB:
            break
        info = animation.probe(path, FFPROBE_BIN)
        if info is None or not (forced or animation.is_gif_like(path, info)):
            continue
        if animation.ready_as_is(path, info):
            ready.append((path, info))
            continue
        trimmed = trimmed or (info.duration or 0) > animation.GIF_MAX_SECONDS
        target = workdir / f"gif_{len(ready) + too_big}.mp4"
        animation.convert(path, target, ffmpeg=FFMPEG_BIN)
        if target.stat().st_size > animation.BOT_ANIMATION_LIMIT:
            animation.convert(path, target, ffmpeg=FFMPEG_BIN, max_side=480)
        if target.stat().st_size > animation.BOT_ANIMATION_LIMIT:
            too_big += 1
            continue
        ready.append((target, animation.probe(target, FFPROBE_BIN)))
    return ready, too_big, trimmed


async def send_job_gifs(app: Application, job: dict) -> None:
    """Send a finished job's GIFs into the chat as Telegram GIFs (animations)."""
    if job.get("gifs_sent") or job.get("status") != "completed":
        return
    if job.get("provider") not in GIF_PROVIDERS:
        return
    if not job.get("gif") and not get_user_settings(job.get("user_id") or 0).get(
        "send_gifs_to_chat", True
    ):
        return
    job["gifs_sent"] = True
    sent = 0
    with tempfile.TemporaryDirectory(prefix="gifs-") as tmp:
        try:
            gifs, too_big, trimmed = await asyncio.to_thread(prepare_gifs, job, Path(tmp))
        except Exception as exc:
            logger.warning("GIF conversion failed for job #%s: %s", job.get("id"), exc)
            job["gif_note"] = "Couldn't turn it into a GIF; the file is on the server."
            return
        for path, info in gifs:
            try:
                with open(path, "rb") as fh:
                    await app.bot.send_animation(
                        chat_id=job["chat_id"],
                        animation=fh,
                        width=getattr(info, "width", None) or None,
                        height=getattr(info, "height", None) or None,
                        duration=int(getattr(info, "duration", 0) or 0) or None,
                        caption=shorten(clean_download_name(job.get("name") or ""), 200) or None,
                        read_timeout=300,
                        write_timeout=300,
                    )
                sent += 1
            except Exception as exc:
                logger.warning("Could not send GIF for job #%s: %s", job.get("id"), exc)
    notes = []
    if sent:
        notes.append(f"🎞 Sent {'as a GIF' if sent == 1 else f'{sent} GIFs'} above")
    if trimmed:
        notes.append(f"first {animation.GIF_MAX_SECONDS} s only")
    if too_big:
        notes.append(f"{too_big} too big for a GIF")
    if job.get("gif") and not sent and not too_big:
        notes.append("Couldn't make a GIF from this")
    if notes:
        job["gif_note"] = " · ".join(notes)


async def finish_job_card(app: Application, job: dict):
    """Show the job's final state as a fresh card at the bottom of the chat.

    A new message (instead of an edit) means Telegram notifies the user that
    the download finished; the progress card it replaces is deleted. GIFs the
    job produced are sent first, so the card stays the last message.
    """
    if job.get("card_final") == job.get("status") or not job.get("status_visible", True):
        return
    await send_job_gifs(app, job)
    old = job.get("card")
    settings = get_user_settings(job.get("user_id") or 0)
    auto_upload = (
        job.get("status") == "completed"
        and settings.get("auto_upload_after_download")
        and bool(job_outputs(job))
    )
    if auto_upload:
        job["auto_upload_note"] = "📤 Uploading to Saved Messages…"
    try:
        await _send_card(app, job)
        job["card_final"] = job.get("status")
    except Exception as exc:
        logger.warning("Could not send final card for #%s: %s", job.get("id"), exc)
        return
    if old:
        try:
            await app.bot.delete_message(chat_id=old["chat_id"], message_id=old["message_id"])
        except Exception as exc:
            logger.debug("Old job card not deleted: %s", exc)
    await update_status_message(app, job["chat_id"], job.get("user_id"))
    if auto_upload:
        await start_job_upload(app, job)


async def start_job_upload(app: Application, job: dict):
    files: list[str] = []
    for rel in job_outputs(job):
        full = DOWNLOAD_DIR / rel
        files += get_all_files_in_folder(rel) if full.is_dir() else [rel]
    if not files:
        return False
    msg = await app.bot.send_message(
        chat_id=job["chat_id"], text=f"{ICON_UPLOAD} Preparing upload of {len(files)} file(s)..."
    )
    run_in_background(
        upload_files_via_pyrogram(
            app, job["chat_id"], msg.message_id, files,
            title=f"Job #{job['id']}: {shorten(clean_download_name(job.get('name', '')), 60)}",
            user_id=job.get("user_id"),
        ),
        name=f"upload-job-{job['id']}",
        on_error=message_error_reporter(app, job["chat_id"], msg.message_id, "Upload"),
    )
    return True


def delete_job_outputs(job: dict) -> int:
    removed = 0
    for rel in job_outputs(job):
        try:
            delete_path(rel)
            removed += 1
        except (OSError, ValueError) as exc:
            logger.warning("Could not delete %s: %s", rel, exc)
        # Drop folders the deletion left empty, up to Download/.
        parent = (DOWNLOAD_DIR / rel).parent
        while parent != DOWNLOAD_DIR and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return removed


async def restart_job(app: Application, job: dict) -> dict | None:
    """Start the same download again (Retry on a failed card)."""
    chat_id, user_id = job["chat_id"], job.get("user_id")
    provider = job.get("provider")
    if provider == "yt-dlp":
        return await start_ytdlp_download(
            app, chat_id, job["url"], audio_only=bool(job.get("audio_only")),
            user_id=user_id, run_in_background=True, max_height=job.get("max_height"),
            gif=bool(job.get("gif")),
        )
    if provider == "spotify":
        return await start_spotify_download(app, chat_id, job["url"], user_id)
    if provider == "manga":
        return await start_manga_download(app, chat_id, job["url"], user_id)
    if provider == "gallery-dl":
        return await start_gallery_download(app, chat_id, job["url"], job.get("category", "gallery"), user_id)
    if not provider and job.get("source"):
        return await start_aria2_download(app, chat_id, job["source"], user_id)
    return None


@auto_answer
async def handle_job_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons on job cards and the status dashboard: job:<action>:<id>."""
    query = update.callback_query
    app = context.application
    _, action, raw_id = (query.data.split(":", 2) + ["", ""])[:3]
    job = download_jobs.get(int(raw_id)) if raw_id.isdigit() else None
    if job is None:
        await answer_once(query, "This job is no longer in the list.", show_alert=True)
        return

    def is_card_message() -> bool:
        card = job.get("card") or {}
        return card.get("message_id") == query.message.message_id

    if action in ("pause", "resume"):
        ok, msg = await (pause_job if action == "pause" else resume_job)(job["id"])
        await answer_once(query, msg, show_alert=not ok)
        await refresh_job_card(app, job, force=True)

    elif action == "cc":
        await refresh_job_card(app, job, force=True, confirm="cancel")

    elif action == "cn":
        await refresh_job_card(app, job, force=True)

    elif action == "cy":
        ok, msg = await cancel_job(job["id"])
        await answer_once(query, msg, show_alert=not ok)
        if ok:
            text, markup = render_job_card(job)
            job["card_final"] = job.get("status")
            await safe_edit_message(query.message, text, markup, parse_mode=ParseMode.HTML)
            await update_status_message(app, job["chat_id"], job.get("user_id"))

    elif action == "show":
        # Bring the card down to the bottom of the chat.
        old = job.get("card")
        await _send_card(app, job)
        if old:
            try:
                await app.bot.delete_message(chat_id=old["chat_id"], message_id=old["message_id"])
            except Exception as exc:
                logger.debug("Old job card not deleted: %s", exc)

    elif action == "up":
        if not await start_job_upload(app, job):
            await answer_once(query, "The downloaded files are no longer there.", show_alert=True)

    elif action in ("del", "deln"):
        text, markup = render_job_card(job, confirm="delete" if action == "del" else None)
        await safe_edit_message(query.message, text, markup, parse_mode=ParseMode.HTML)

    elif action == "dely":
        removed = await asyncio.to_thread(delete_job_outputs, job)
        await answer_once(query, f"Deleted {removed} item(s).")
        await safe_edit_message(
            query.message,
            f"🗑 <b>{html.escape(shorten(clean_download_name(job.get('name', '')), 80))}</b>\n"
            f"#{job['id']} · files deleted from the server",
            None,
            parse_mode=ParseMode.HTML,
        )

    elif action == "retry":
        try:
            new_job = await restart_job(app, job)
        except Exception as exc:
            await answer_once(query, f"Couldn't start it again: {shorten(str(exc), 150)}", show_alert=True)
            return
        if new_job is None:
            await answer_once(query, "This kind of download can't be retried; send the link again.", show_alert=True)
            return
        await answer_once(query, "Starting again…")
        await attach_job_card(app, new_job, message=query.message if is_card_message() else None)

    elif action == "dismiss":
        try:
            await query.message.delete()
        except Exception:
            await safe_edit_message(query.message, "Dismissed.")


def shorten(text: str, max_len: int = 38) -> str:
    text = text.strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


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


async def safe_edit_message(message, text, reply_markup=None, parse_mode=None):
    try:
        await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await message.reply_text(
                text=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
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


MINI_APP = home_views.MiniApp(url=WEB_APP_URL, enabled=WEB_APP_ENABLE)
# Taps on the bottom keyboard: they always act as buttons, even while a prompt waits for text.
KEYBOARD_LABELS = {
    home_views.KEY_STATUS,
    home_views.KEY_FILES,
    home_views.KEY_SEARCH,
    home_views.KEY_SETTINGS,
    home_views.KEY_MENU,
    home_views.KEY_HELP,
}


def mini_app_inline_button(label: str = "📱 Mini App"):
    """HTTPS-aware inline Mini App button; falls back to a plain URL button."""
    return MINI_APP.inline_button(label)


def build_reply_menu(user_id: int = None):
    return home_views.reply_keyboard(MINI_APP)


def free_disk_bytes() -> int | None:
    try:
        return shutil.disk_usage(DOWNLOAD_DIR).free
    except OSError:
        return None


def active_job_count() -> int:
    return sum(
        1
        for job in download_jobs.values()
        if job.get("status") in JOB_ACTIVE_STATES and job.get("status_visible", True)
    )


def build_home_screen() -> Screen:
    return home_views.home_screen(active_job_count(), free_disk_bytes(), MINI_APP)


def build_settings_screen(user_id: int) -> Screen:
    return settings_views.settings_screen(get_user_settings(user_id), current_cookies())


def current_cookies():
    path = cookies_file()
    return cookie_store.read_summary(Path(path)) if path else None


def build_cookies_screen(context, *, confirm_delete: bool = False) -> Screen:
    path = cookies_file()
    return settings_views.cookies_screen(
        current_cookies(),
        waiting=cookies_waiting(context),
        from_env=bool(path) and Path(path) != UPLOADED_COOKIES_PATH,
        confirm_delete=confirm_delete,
    )


COOKIE_WAIT_SECONDS = 600


def cookies_waiting(context) -> bool:
    since = context.user_data.get("cookies_wait")
    return bool(since) and time.time() - since < COOKIE_WAIT_SECONDS


def looks_like_cookie_file(name: str | None) -> bool:
    name = (name or "").lower()
    return "cookie" in name and name.endswith(".txt")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Non-torrent files: only a cookies.txt is expected (Settings → Cookies)."""
    message = update.message
    doc = message.document
    if not (cookies_waiting(context) or looks_like_cookie_file(doc.file_name)):
        return  # e.g. forwarded files, which the Pyrogram side handles
    if (doc.file_size or 0) > cookie_store.MAX_BYTES:
        await message.reply_text("❌ That file is too big for a cookies.txt (over 2 MB).")
        return
    file = await doc.get_file()
    data = bytes(await file.download_as_bytearray())
    # The file holds a login; don't leave it in the chat history.
    try:
        await message.delete()
        removed = "I deleted your message with the file."
    except Exception as exc:
        logger.debug("Could not delete the cookies message: %s", exc)
        removed = "Delete your message with the file yourself; I couldn't."
    try:
        text, summary = cookie_store.parse_cookies(data)
    except cookie_store.CookieFileError as exc:
        await message.chat.send_message(f"❌ {exc}\n\n{removed}")
        return
    await asyncio.to_thread(cookie_store.save_cookies, UPLOADED_COOKIES_PATH, text)
    context.user_data.pop("cookies_wait", None)
    sites = ", ".join(summary.sites[:4])
    lines = [f"🍪 Saved {summary.count} cookies for {html.escape(sites)}.", removed]
    if "youtube.com" not in summary.sites:
        lines.append("⚠️ There are no youtube.com cookies in it, so YouTube stays blocked.")
    if summary.expired:
        lines.append(f"⚠️ {summary.expired} have already expired.")
    lines.append("Send the link again (or tap Retry on a failed card).")
    await message.chat.send_message("\n".join(lines), parse_mode=ParseMode.HTML)


def build_archive_settings_screen(user_id: int, context=None) -> Screen:
    back = (context.user_data.get("archive_settings_back") if context else None) or "nav:settings"
    return settings_views.archive_settings_screen(get_user_settings(user_id), back)


async def show_screen(message, screen: Screen, *, edit: bool = True):
    """Show an HTML view: edit ``message`` in place, or reply with a new message."""
    text, markup = screen
    if edit:
        try:
            return await message.edit_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return message
            logger.debug("Screen edit failed (%s); sending a new message", exc)
    return await message.reply_text(
        text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


def format_forwarded_posts_setting(user_id: int) -> str:
    settings = get_user_settings(user_id)
    enabled = settings.get("auto_download_forwarded_posts", False)
    return f"Forwarded media auto-download: {'ON' if enabled else 'OFF'}"


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
            is_dir = entry.is_dir()
            count = 0
            if is_dir:
                try:
                    count = sum(1 for _ in os.scandir(entry))
                except OSError:
                    count = 0
            items.append({
                "name": entry.name,
                "rel_path": os.path.join(rel_path, entry.name) if rel_path else entry.name,
                "is_dir": is_dir,
                "size": st.st_size if entry.is_file() else 0,
                "mtime": st.st_mtime,
                "count": count,
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


# Library folders the bot manages itself; "Organize" at the Download root skips them.
ORGANIZE_PROTECTED_NAMES = {"Telegram", "Spotify", "Manga", "Adult", "Hentai", "Gallery", "_torrents"}


def active_job_names() -> set[str]:
    """Names of running jobs. Call on the event loop: download_jobs changes there."""
    names: set[str] = set()
    for job in list(download_jobs.values()):
        if job.get("status") in JOB_ACTIVE_STATES:
            name = str(job.get("name", ""))
            names.update({name, clean_download_name(name)})
    return names


def build_organize_plan(rel_path: str, busy_names: set[str]) -> OrganizePlan:
    folder = safe_join(DOWNLOAD_DIR, rel_path)
    return plan_organize(
        folder,
        protected_names=ORGANIZE_PROTECTED_NAMES if not rel_path else (),
        busy_names=busy_names,
    )


def build_organize_preview(rel_path: str, page: int, plan: OrganizePlan):
    shown_path = "/" + rel_path.lstrip("/") if rel_path else "/"
    encoded = encode_path(rel_path)
    back = InlineKeyboardButton(f"{ICON_BACK} Back", callback_data=f"fb:list:{page}:{encoded}")
    lines = [f"{ICON_BROOM} Organize videos", f"{ICON_PIN} {shown_path}", ""]
    if not plan.moves:
        lines.append("Nothing to organize: no videos in sub-folders of this folder.")
        if plan.skipped_busy:
            lines.append(f"Skipped {len(plan.skipped_busy)} folder(s) with downloads in progress.")
        return "\n".join(lines), InlineKeyboardMarkup([[back]])

    extra_files = len(plan.moves) - plan.video_count
    lines.append(
        f"Move {plan.video_count} video(s) from {len(plan.folders)} sub-folder(s) into this folder"
        + (f", with {extra_files} matching subtitle file(s)." if extra_files else ".")
    )
    for _, dst in plan.moves[:8]:
        lines.append(f"• {shorten(dst.name, 60)}")
    if len(plan.moves) > 8:
        lines.append(f"… and {len(plan.moves) - 8} more")
    lines.append("")
    if plan.leftover_files:
        lines.append(
            f"Other files left behind: {plan.leftover_files} ({human_size(plan.leftover_bytes)}). "
            "They are kept unless you choose to delete them."
        )
    lines.append("Folders that end up empty are removed.")
    if plan.skipped_busy:
        lines.append(f"Skipped {len(plan.skipped_busy)} folder(s) with downloads in progress.")

    rows = [[InlineKeyboardButton(
        f"{ICON_OK} Move {plan.video_count} video(s)",
        callback_data=f"fb:organize_go:{page}:{encoded}:keep",
    )]]
    if plan.leftover_files:
        rows.append([InlineKeyboardButton(
            f"{ICON_DELETE} Move + delete leftovers ({human_size(plan.leftover_bytes)})",
            callback_data=f"fb:organize_go:{page}:{encoded}:purge",
        )])
    rows.append([back])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def show_folder(message, rel_path: str, page: int = 0, *, edit: bool = True):
    entries = await asyncio.to_thread(list_dir, rel_path)
    await show_screen(
        message, file_views.browser_screen(rel_path, entries, page, encode_path), edit=edit
    )


async def show_file(message, rel_path: str, page: int = 0):
    info = await asyncio.to_thread(file_info, rel_path)
    if info["is_dir"]:
        await show_folder(message, rel_path, 0)
        return
    await show_screen(message, file_views.file_screen(
        rel_path, info["size"], info["mtime"], page, encode_path,
        is_video=is_video_file(info["name"]),
    ))


def files_in_folder(rel_path: str) -> list[dict]:
    return [entry for entry in list_dir(rel_path) if not entry["is_dir"]]


async def show_selection(message, user_id: int):
    session = batch_select_sessions[user_id]
    files = await asyncio.to_thread(files_in_folder, session["rel_path"])
    session["selected"] &= {f["rel_path"] for f in files}  # forget deleted files
    await show_screen(message, file_views.select_screen(
        session["rel_path"], files, session["selected"], session["page"], encode_path
    ))


async def handle_file_selection(update: Update, context, action: str, parts: list[str]):
    """Selection mode in the file browser: pick files, then upload, zip or delete them."""
    query = update.callback_query
    user_id = query.from_user.id
    session = batch_select_sessions.get(user_id)
    if session is None:
        await answer_once(query, "This selection has expired. Open Files again.", show_alert=True)
        return
    if action == "bupload_empty":
        await answer_once(query, "Select at least one file first.", show_alert=True)
        return

    if action == "st":
        token = parts[2] if len(parts) > 2 else ""
        session["selected"].symmetric_difference_update({decode_path(token)})
    elif action == "sp":
        page_raw = parts[2] if len(parts) > 2 else "0"
        if page_raw != "-1":
            session["page"] = int(page_raw)
    elif action in ("sall", "snone"):
        files = await asyncio.to_thread(files_in_folder, session["rel_path"])
        session["selected"] = {f["rel_path"] for f in files} if action == "sall" else set()
    elif action == "sdone":
        batch_select_sessions.pop(user_id, None)
        await show_folder(query.message, session["rel_path"], session["page"])
        return
    else:
        selected = sorted(session["selected"])
        if not selected:
            await answer_once(query, "Select at least one file first.", show_alert=True)
            return
        if action == "sup":
            batch_select_sessions.pop(user_id, None)
            await answer_once(query)
            await safe_edit_message(query.message, f"{ICON_UPLOAD} Preparing upload of {len(selected)} file(s)...")
            run_in_background(
                send_folder_files_via_pyrogram(
                    context.application, query.message.chat_id, query.message.message_id,
                    session["rel_path"], file_list=selected, user_id=user_id,
                ),
                name="upload",
                on_error=message_error_reporter(
                    context.application, query.message.chat_id, query.message.message_id, "Upload"
                ),
            )
            return
        if action == "szip":
            paths = filter_files_for_archiving([safe_join(DOWNLOAD_DIR, rel) for rel in selected])
            if not paths:
                await answer_once(query, "None of the selected files can be zipped.", show_alert=True)
                return
            await ask_zip_name(
                query.message, user_id, query.message.chat_id, paths,
                default_name=rel_name(session["rel_path"]) if session["rel_path"] else default_archive_name(),
                cancel_data="fb:sp:-1",
            )
            return
        if action == "sdel":
            infos = [await asyncio.to_thread(file_info, rel) for rel in selected]
            await show_screen(query.message, file_views.delete_selected_confirm_screen(
                [info["name"] for info in infos], sum(info["size"] for info in infos)
            ))
            return
        if action == "sdelyes":
            deleted, errors = await asyncio.to_thread(delete_paths_batch, selected)
            session["selected"] = set()
            await answer_once(
                query,
                f"Deleted {deleted} file(s)." + (f" {len(errors)} could not be deleted." if errors else ""),
                show_alert=bool(errors),
            )
    await show_selection(query.message, user_id)


def is_manga_gallery_folder(folder: Path) -> bool:
    try:
        folder.relative_to(MANGA_DIR)
    except ValueError:
        return False
    return folder.is_dir() and bool(list_manga_images(folder))


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
    
    copy_buffer_size = 8 * 1024 * 1024
    with open(full, "rb") as src:
        while chunk_num <= total_chunks:
            chunk_path = chunk_dir / f"{full.stem}_part_{chunk_num:03d}{full.suffix}"
            with open(chunk_path, "wb") as dst:
                remaining = min(chunk_size, file_size - src.tell())
                while remaining > 0:
                    block = src.read(min(copy_buffer_size, remaining))
                    if not block:
                        break
                    dst.write(block)
                    remaining -= len(block)

            chunks.append((str(chunk_path), chunk_num, total_chunks))
            chunk_num += 1
    
    return chunks


def cleanup_file_chunks(chunks: list) -> None:
    """Delete temporary split-upload parts and the folder holding them.

    split_file_into_chunks() copies the whole file into <stem>_chunks/ when it
    exceeds the Telegram part limit, so skipping this leaves a full duplicate
    on disk for every large upload.
    """
    chunk_dirs: set[Path] = set()
    for chunk_path, _, chunk_total in chunks:
        if chunk_total <= 1:
            continue
        chunk = Path(chunk_path)
        chunk_dirs.add(chunk.parent)
        try:
            if chunk.exists():
                chunk.unlink()
        except OSError:
            logger.warning("Could not remove temporary upload chunk: %s", chunk)

    for chunk_dir in chunk_dirs:
        try:
            chunk_dir.rmdir()
        except OSError:
            pass


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

def parse_ffmpeg_duration(ffmpeg_stderr: str) -> float | None:
    for line in ffmpeg_stderr.splitlines():
        if "Duration:" in line:
            try:
                h, m, sec = line.split("Duration:")[1].split(",")[0].strip().split(":")
                return int(h) * 3600 + int(m) * 60 + float(sec)
            except (ValueError, IndexError):
                return None
    return None


def parse_ffmpeg_progress_seconds(line: str) -> float | None:
    """Seconds encoded so far from an ``-progress`` line (out_time_us/out_time_ms are µs)."""
    key, _, value = line.strip().partition("=")
    if key in ("out_time_us", "out_time_ms"):
        try:
            return int(value) / 1_000_000
        except ValueError:
            return None
    return None


async def convert_video_quality(input_path: str, output_path: str, target_res: str, progress_callback=None):
    """
    Re-encode video to given resolution (e.g. '720p') with libx264, CRF 23.

    Progress comes from ``-progress pipe:1``: ffmpeg's normal stderr status line
    is redrawn with carriage returns, so reading it line by line only returned
    once the encode had finished and the progress bar stayed at 0%.
    """
    resolution_map = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
    target_h = resolution_map.get(target_res, 720)

    probe = await asyncio.to_thread(
        subprocess.run,
        [FFMPEG_BIN, "-i", input_path],
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    duration = parse_ffmpeg_duration(probe.stderr)

    cmd = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel", "error",
        "-nostats",
        "-progress", "pipe:1",
        "-i", input_path,
        "-vf", f"scale=-2:{target_h}",
        "-c:v", "libx264",
        "-crf", "23",
        "-preset", "medium",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-y",
        output_path,
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stderr_task = asyncio.create_task(process.stderr.read())
    last_update = 0.0
    try:
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            seconds = parse_ffmpeg_progress_seconds(raw.decode("utf-8", errors="ignore"))
            if progress_callback and duration and seconds is not None:
                now = time.time()
                if now - last_update > 3.0:
                    last_update = now
                    await progress_callback(min(99, int(seconds / duration * 100)))
        await process.wait()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        Path(output_path).unlink(missing_ok=True)
        raise
    stderr = (await stderr_task).decode("utf-8", errors="ignore").strip()
    if process.returncode != 0:
        Path(output_path).unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg conversion failed: {stderr[-300:] or process.returncode}")


async def send_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE, rel_path: str):
    """Send a contact sheet (grid of frames from across the video) as a photo."""
    full = safe_join(DOWNLOAD_DIR, rel_path)
    chat_id = update.effective_chat.id

    if not full.exists() or not is_video_file(str(full)):
        await context.bot.send_message(chat_id=chat_id, text=f"{ICON_WARN} {full.name} is not a video file.")
        return

    tmp_dir = Path(tempfile.gettempdir()) / f"thumb_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    output_path = tmp_dir / "contact_sheet.jpg"
    try:
        # Decoding frames is CPU-heavy; keep it off the event loop.
        await asyncio.to_thread(generate_contact_sheet, str(full), str(output_path))
        if not output_path.exists():
            raise RuntimeError("no image was produced")
        with open(output_path, "rb") as img:
            await context.bot.send_photo(
                chat_id=chat_id, photo=img, caption=f"📸 Thumbnails: {full.name}"
            )
    except Exception as exc:
        logger.warning("Thumbnail generation failed for %s: %s", full, exc)
        await context.bot.send_message(
            chat_id=chat_id, text=f"{ICON_FAIL} Could not make thumbnails for {full.name}: {exc}"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# =========================================================
# yt-dlp video downloader
# =========================================================

def cookies_file() -> str | None:
    """The cookies.txt to use: the one sent to the bot, else YTDLP_COOKIES_FILE."""
    if UPLOADED_COOKIES_PATH.is_file():
        return str(UPLOADED_COOKIES_PATH)
    if YTDLP_COOKIES_FILE and Path(YTDLP_COOKIES_FILE).is_file():
        return YTDLP_COOKIES_FILE
    return None


# yt-dlp needs a JavaScript runtime for YouTube; it only looks for Deno by default.
YTDLP_JS_RUNTIMES = {
    name: {} for name in ("deno", "node", "bun") if shutil.which(name)
}


def ytdlp_common_options(url: str, resolved_video) -> dict:
    """yt-dlp options shared by downloads and the quality probe."""
    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if cookies := cookies_file():
        opts["cookiefile"] = cookies
    if YTDLP_JS_RUNTIMES:
        opts["js_runtimes"] = dict(YTDLP_JS_RUNTIMES)
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
    if requires_ytdlp_generic_impersonation(url):
        opts["extractor_args"] = {"generic": {"impersonate": ["chrome"]}}
    if resolved_video.referer:
        opts["http_headers"]["Referer"] = resolved_video.referer
    return opts


def probe_video(url: str) -> dict:
    """Title, duration and available video heights, without downloading."""
    resolved = resolve_adult_video_url(url)
    opts = ytdlp_common_options(url, resolved)
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(resolved.url, download=False) or {}
    heights = sorted({
        int(fmt["height"])
        for fmt in info.get("formats") or []
        if fmt.get("height") and fmt.get("vcodec") not in (None, "none")
    })
    if not heights and info.get("height"):
        heights = [int(info["height"])]
    return {"title": info.get("title") or "", "duration": info.get("duration"), "heights": heights}


QUALITY_STEPS = (1080, 720, 480)
GIF_MAX_HEIGHT = 720

_COOKIE_HINT = "Add cookies from a logged-in browser in ⚙️ Settings → 🍪 Cookies, then retry."
# (phrase in yt-dlp's error, plain explanation). These fail the same way on retry.
YTDLP_KNOWN_ERRORS = (
    (
        "confirm you're not a bot",
        "YouTube is blocking downloads from this server's IP address. " + _COOKIE_HINT,
    ),
    ("confirm your age", "This video is age-restricted. " + _COOKIE_HINT),
    ("age-restricted", "This video is age-restricted. " + _COOKIE_HINT),
    ("private video", "This video is private. " + _COOKIE_HINT),
    ("members-only", "This video is for channel members only. " + _COOKIE_HINT),
    ("video unavailable", "This video is unavailable (removed, blocked or region-locked)."),
    ("unsupported url", "This link isn't supported by yt-dlp."),
)


def explain_ytdlp_error(error) -> tuple[str, bool]:
    """A short, readable reason for a yt-dlp error, and whether retrying is pointless."""
    text = " ".join(str(error).split())
    normalized = text.replace("\u2019", "'").lower()
    for phrase, explanation in YTDLP_KNOWN_ERRORS:
        if phrase in normalized:
            return explanation, True
    text = re.sub(r"^ERROR:\s*", "", text)
    text = re.sub(r"^\[[^\]]+\]\s*[^:\s]+:\s*", "", text)  # "[youtube] abc123: "
    text = re.split(r"\s(?:Use --|See https?://)", text, maxsplit=1)[0]
    return text or "Unknown error", False


class DownloadFailed(RuntimeError):
    """A failure whose readable reason is already worked out."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


cobalt_client = CobaltClient(COBALT_API_URL, COBALT_API_KEY)
COBALT_UNSUPPORTED = {"error.api.link.unsupported", "error.api.service.unsupported"}


async def download_with_cobalt(job: dict, url: str, output_dir: Path, ytdlp_error, is_cancelled):
    """Second try through cobalt after yt-dlp failed. Returns (title, filepath or None)."""
    ytdlp_reason, _ = explain_ytdlp_error(ytdlp_error)
    logger.info("yt-dlp failed for %s (%s); trying cobalt", url, ytdlp_reason)
    job.update(engine="cobalt", note="via cobalt", status="downloading", last_line="")
    try:
        items = await cobalt_client.resolve(
            url,
            audio_only=bool(job.get("audio_only")),
            mute=bool(job.get("gif")),
            max_height=job.get("max_height"),
        )
        destination = output_dir
        if len(items) > 1:
            destination = output_dir / f"cobalt_{job['id']}"
        paths: list[Path] = []
        finished_bytes = 0
        for item in items:
            def progress(done: int, total: int, base: int = finished_bytes):
                job["completed_length"] = base + done
                job["total_length"] = base + total if total else 0

            path = await cobalt_client.download(
                item, destination, progress=progress, is_cancelled=is_cancelled
            )
            paths.append(path)
            finished_bytes += path.stat().st_size
    except CobaltError as exc:
        if exc.code == "cancelled":
            raise
        if exc.code in COBALT_UNSUPPORTED:
            raise DownloadFailed(ytdlp_reason) from exc
        raise DownloadFailed(f"{ytdlp_reason}\n\ncobalt: {exc}") from exc
    job["completed_length"] = job["total_length"] = finished_bytes
    if len(paths) == 1:
        return paths[0].stem, str(paths[0])
    job["outputs"] = [str(destination)]
    return f"{len(paths)} files from {job.get('platform') or 'the post'}", None


def explain_spotdl_error(error) -> tuple[str, bool]:
    """(readable reason, whether YouTube cookies would help) for a spotDL failure."""
    text = " ".join(str(error).split())
    lowered = text.lower()
    if any(s in lowered for s in ("audioprovidererror", "yt-dlp download error", "no usable results")):
        return (
            "spotDL found the song on Spotify, but it gets the audio from YouTube and YouTube "
            "refused this server. " + _COOKIE_HINT,
            True,
        )
    return text[-400:] or "spotDL failed", False


def quality_actions(heights: list[int], duration: float | None = None) -> list[tuple[str, str]]:
    """Picker buttons: Best (with its height when known), lower standard heights, MP3,
    and GIF for clips of up to a minute."""
    top = max(heights) if heights else None
    actions = [(f"⭐ Best ({top}p)" if top else "⭐ Best", "best")]
    for h in QUALITY_STEPS:
        # Offer a cap only when it differs from Best (the video has more than h).
        if top is None and h == 720 or (top is not None and top > h):
            actions.append((f"{h}p", f"h{h}"))
    actions.append(("🎵 MP3", "mp3"))
    if duration is None or duration <= animation.GIF_MAX_SECONDS:
        actions.append(("🎞 GIF", "gif"))
    return actions


async def show_quality_picker(message, rid: str, url: str, platform: str):
    """Fill the video prompt with what the video actually offers."""
    try:
        info = await asyncio.wait_for(asyncio.to_thread(probe_video, url), timeout=60)
    except Exception as exc:
        logger.info("Quality probe failed for %s: %s", url, exc)
        reason, fatal = explain_ytdlp_error(exc)
        if fatal:
            if link_requests.pop(rid, None) is None:
                return  # cancelled while we were looking
            markup = None
            if _COOKIE_HINT in reason:
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🍪 Add cookies", callback_data="nav:cookies")]]
                )
            await safe_edit_message(
                message,
                f"❌ <b>Can't download this {html.escape(platform)} link</b>\n\n"
                f"{html.escape(reason)}",
                markup,
                parse_mode=ParseMode.HTML,
            )
            return
        info = {"title": "", "duration": None, "heights": []}
    if rid not in link_requests:
        return  # cancelled while we were looking
    lines = [f"🎬 <b>{html.escape(shorten(info['title'] or platform + ' video', 90))}</b>"]
    meta = platform
    if info.get("duration"):
        meta += f" · {job_views.duration(info['duration'])}"
    lines += [html.escape(meta), "", "Choose a quality:"]
    if not info["heights"]:
        lines.append("<i>(Couldn't list the qualities; Best picks the highest available.)</i>")
    await safe_edit_message(
        message,
        "\n".join(lines),
        link_request_markup(rid, quality_actions(info["heights"], info.get("duration"))),
        parse_mode=ParseMode.HTML,
    )


def video_default_choice(user_id: int) -> tuple[bool, int | None] | None:
    """(audio_only, max_height) from Settings → Video links, or None to ask."""
    value = str(get_user_settings(user_id).get("video_default", "ask"))
    if value == "mp3":
        return True, None
    if value == "best":
        return False, None
    if value.isdigit():
        return False, int(value)
    return None


def video_format_selector(max_height: int | None) -> str:
    """yt-dlp format: best video+audio, optionally capped at ``max_height``.

    Falls back to the best available format so a cap never makes a download fail.
    """
    if not max_height:
        return "bv*+ba/b"
    h = int(max_height)
    return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"


def is_video_url(text: str) -> bool:
    text = extract_http_url(text).lower()
    return is_supported_video_url(text)


def build_hentai_playlist_prompt_text(playlist: HentaiPlaylist, user_id: int = 0) -> str:
    mode = get_user_settings(user_id).get("batch_download_mode")
    return (
        f"{ICON_DOWNLOAD} Hentai playlist detected\n\n"
        f"Site: {playlist.site}\n"
        f"Title:\n{shorten(clean_download_name(playlist.title), 100)}\n"
        f"Episodes: {len(playlist.urls)}\n\n"
        f"Batch mode: {batch_download_mode_label(mode)}\n"
        f"{batch_download_mode_description(mode)}\n\n"
        f"Folder:\n{HENTAI_VIDEO_DIR / video_platform_slug(playlist.urls[0])}"
    )


def build_pornhub_model_prompt_text(playlist: PornHubModelPlaylist, user_id: int = 0) -> str:
    mode = get_user_settings(user_id).get("batch_download_mode")
    return (
        f"{ICON_DOWNLOAD} PornHub model page detected\n\n"
        f"Model:\n{shorten(clean_download_name(playlist.slug), 100)}\n"
        f"Videos found: {len(playlist.urls)}\n\n"
        f"Batch mode: {batch_download_mode_label(mode)}\n"
        f"{batch_download_mode_description(mode)}\n\n"
        f"Folder:\n{ADULT_VIDEO_DIR / 'PornHub'}"
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


def build_manga_prompt_text(url: str) -> str:
    return (
        f"{ICON_IMAGE} Manga/gallery link detected\n\n"
        "Download this gallery?\n\n"
        f"Folder:\n{MANGA_DIR}\n\n"
        f"Link:\n{shorten(url, 160)}"
    )


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
            await finish_job_card(app, job)
        except Exception as e:
            logger.exception("Manga download failed")
            job["status"] = "failed"
            job["finished_at"] = now_ts()
            job["last_line"] = str(e)
            await maybe_auto_update_status_message(app, job, force=True)
            await finish_job_card(app, job)

    job["task"] = run_in_background(run_job(), name=f"manga-{job_id}")
    return job


async def start_gallery_download(
    app: Application, chat_id: int, url: str, category: str, user_id: int = None
):
    """Download an image gallery/post with gallery-dl into Download/Gallery/<site>/."""
    global job_counter

    async with jobs_lock:
        job_counter += 1
        job_id = job_counter

    job = {
        "id": job_id,
        "provider": "gallery-dl",
        "category": category,
        "name": f"{site_label(category)} · {shorten(url.split('://', 1)[-1], 60)}",
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
        "eta": "Unknown",
        "started_at": now_ts(),
        "finished_at": None,
        "last_line": "Looking for files…",
        "outputs": [],
    }
    download_jobs[job_id] = job

    def set_process(process):
        job["process"] = process
        job["pid"] = process.pid

    def on_file(path: Path):
        job["status"] = "downloading"
        job["outputs"].append(str(path))
        try:
            job["completed_length"] += path.stat().st_size
        except OSError:
            pass
        job["last_line"] = f"{len(job['outputs'])} file(s) so far · {path.name}"

    async def run_job():
        refresher = asyncio.create_task(_refresh_while_active(app, job))
        try:
            files = await download_gallery(
                url,
                GALLERY_DIR,
                on_file=on_file,
                set_process=set_process,
                cookies_file=cookies_file(),
                proxy=YTDLP_PROXY or None,
            )
            if job.get("status") == "cancelled":
                return
            job["outputs"] = [str(f) for f in files]
            job["status"] = "completed"
            job["progress"] = 100.0
            job["finished_at"] = now_ts()
            job["last_line"] = f"{len(files)} file(s)"
            await finish_job_card(app, job)
        except Exception as exc:
            if job.get("status") == "cancelled":
                return
            logger.warning("gallery-dl download failed: %s", exc)
            job["status"] = "failed"
            job["finished_at"] = now_ts()
            job["last_line"] = str(exc)
            await finish_job_card(app, job)
        finally:
            job["process"] = None
            refresher.cancel()

    job["task"] = run_in_background(run_job(), name=f"gallery-{job_id}")
    return job


async def _refresh_while_active(app: Application, job: dict):
    """Refresh a job's card while it runs, for providers without their own progress loop."""
    while job.get("status") in JOB_ACTIVE_STATES:
        await maybe_auto_update_status_message(app, job)
        await asyncio.sleep(2)


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
        provider = SpotifyDownloader(
            spotdl_bin=SPOTDL_BIN,
            ffmpeg_bin=FFMPEG_BIN,
            cookie_file=cookies_file(),
            proxy=YTDLP_PROXY or None,
        )
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
            if not result.artifacts:
                job["note"] = "already downloaded"
            job["outputs"] = [
                str(artifact.path) for artifact in result.artifacts if artifact.media_type == "audio"
            ]
            job["finished_at"] = now_ts()
            job["last_line"] = "Completed"
            await maybe_auto_update_status_message(app, job, force=True)
            await finish_job_card(app, job)
        except Exception as e:
            if job.get("status") == "cancelled":
                return
            reason, needs_cookies = explain_spotdl_error(e)
            logger.warning("spotDL download failed: %s", e)
            job["status"] = "failed"
            job["finished_at"] = now_ts()
            job["last_line"] = reason
            job["needs_cookies"] = needs_cookies
            await maybe_auto_update_status_message(app, job, force=True)
            await finish_job_card(app, job)
        finally:
            job["process"] = None

    job["task"] = run_in_background(run_job(), name=f"spotify-{job_id}")
    return job


async def start_ytdlp_download(
    app: Application,
    chat_id: int,
    url: str,
    audio_only: bool = False,
    user_id: int = None,
    run_in_background: bool = False,
    notify: bool = True,
    parent_job: dict | None = None,
    max_height: int | None = None,
    gif: bool = False,
):
    """Download one video with yt-dlp (``max_height`` caps the resolution).

    ``gif`` makes a silent clip that is sent to the chat as a GIF. When yt-dlp
    fails and a cobalt instance is configured, cobalt gets a try.

    ``parent_job`` is the batch (playlist/model page) this item belongs to;
    cancelling the batch aborts the item that is downloading.
    """
    global job_counter, download_jobs

    async with jobs_lock:
        job_counter += 1
        job_id = job_counter

    is_hentai = is_hentai_video_url(url)
    is_adult = is_adult_video_url(url)
    platform = video_platform_label(url)
    if gif:
        audio_only, max_height = False, min(max_height or GIF_MAX_HEIGHT, GIF_MAX_HEIGHT)
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
        "filepath": "",
        "audio_only": audio_only,
        "max_height": None if audio_only else max_height,
        "gif": gif,
    }

    download_jobs[job_id] = job

    loop = asyncio.get_running_loop()

    def is_cancelled() -> bool:
        return job.get("status") == "cancelled" or bool(
            parent_job and parent_job.get("status") == "cancelled"
        )

    def progress_hook(d):
        # Outside the try below: that block logs and swallows exceptions, and
        # this one has to reach yt-dlp to abort the download.
        if is_cancelled():
            raise yt_dlp.utils.DownloadCancelled("Cancelled by user")
        if d.get("tmpfilename"):
            job["tmpfile"] = d["tmpfilename"]
        title = (d.get("info_dict") or {}).get("title")
        if title and job.get("name") == "Fetching video info...":
            job["name"] = title
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
        if requires_deno_runtime(url) and not DENO_VERSION:
            raise RuntimeError(
                "Hanime requires Deno. Install Deno, ensure `deno --version` works for "
                "the bot service user, or set DENO_BIN=/absolute/path/to/deno in .env."
            )
        resolved_video = resolve_adult_video_url(url)
        download_url = resolved_video.url
        existing_files = {
            path.resolve()
            for path in output_dir.iterdir()
            if path.is_file()
        }
        output_template = str(output_dir / "%(title).200B [%(id)s].%(ext)s")
        if resolved_video.referer:
            output_template = resolved_video_output_template(output_dir, url)

        ydl_opts = ytdlp_common_options(url, resolved_video)
        ydl_opts.update({
            "outtmpl": output_template,
            "progress_hooks": [progress_hook],
            "concurrent_fragment_downloads": 4,
            "fragment_retries": 10,
            "file_access_retries": 3,
            "continuedl": True,
            "part": True,
            "windowsfilenames": False,
        })

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
                "format": video_format_selector(max_height),
                "merge_output_format": "mp4",
            })

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(download_url, download=True)

            if info is None:
                raise RuntimeError("Failed to extract video info")

            title = info.get("title") or "Unknown Video"

            filepath_candidates = []
            requested = info.get("requested_downloads") or []
            filepath_candidates.extend(item.get("filepath") for item in requested)
            filepath_candidates.extend(
                [
                    info.get("filepath"),
                    info.get("_filename"),
                    ydl.prepare_filename(info),
                ]
            )

            if audio_only:
                filepath_candidates.extend(
                    f"{os.path.splitext(candidate)[0]}.mp3"
                    for candidate in list(filepath_candidates)
                    if candidate
                )
            else:
                filepath_candidates.extend(
                    f"{os.path.splitext(candidate)[0]}.mp4"
                    for candidate in list(filepath_candidates)
                    if candidate
                )

            filepath = next(
                (
                    str(Path(candidate).resolve())
                    for candidate in filepath_candidates
                    if candidate and Path(candidate).is_file()
                ),
                None,
            )
            if not filepath:
                new_files = [
                    path
                    for path in output_dir.iterdir()
                    if path.is_file()
                    and path.resolve() not in existing_files
                    and path.suffix not in {".part", ".ytdl"}
                ]
                if new_files:
                    filepath = str(max(new_files, key=lambda path: path.stat().st_mtime).resolve())

            return title, filepath

    def remove_partial_files():
        tmp = job.get("tmpfile")
        if not tmp:
            return
        tmp_path = Path(tmp)
        try:
            for leftover in tmp_path.parent.iterdir():
                # Prefix match, not glob(): titles often contain "[...]".
                if leftover.is_file() and leftover.name.startswith(tmp_path.name):
                    leftover.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove partial download %s: %s", tmp, exc)

    async def finish_cancelled():
        job["status"] = "cancelled"
        job["finished_at"] = job.get("finished_at") or now_ts()
        job["last_line"] = "Cancelled by user"
        await asyncio.to_thread(remove_partial_files)
        if notify:
            await finish_job_card(app, job)

    async def finish_download():
        refresh_task = asyncio.create_task(refresh_ytdlp_status())
        try:
            try:
                title, filepath = await loop.run_in_executor(None, run_download)
            except Exception as ytdlp_error:
                if is_cancelled() or not cobalt_client.enabled or is_hentai or is_adult:
                    raise
                title, filepath = await download_with_cobalt(
                    job, url, output_dir, ytdlp_error, is_cancelled
                )
            if is_cancelled():
                await finish_cancelled()
                return

            job["status"] = "completed"
            job["name"] = title
            job["progress"] = 100.0
            if filepath and os.path.exists(filepath):
                size = os.path.getsize(filepath)
                job["completed_length"] = size
                job["total_length"] = size
                job["filepath"] = str(Path(filepath).resolve())
            job["download_speed"] = 0
            job["finished_at"] = now_ts()
            if notify:
                await maybe_auto_update_status_message(app, job, force=True)

            if notify:
                await finish_job_card(app, job)

        except Exception as e:
            if is_cancelled():
                await finish_cancelled()
                return
            if isinstance(e, DownloadFailed):
                reason, fatal = e.reason, True
            else:
                reason, fatal = explain_ytdlp_error(e)
            if fatal:
                logger.warning("yt-dlp download failed: %s", e)
            else:
                logger.exception("yt-dlp download failed")

            job["status"] = "failed"
            job["finished_at"] = now_ts()
            job["last_line"] = reason
            job["needs_cookies"] = _COOKIE_HINT in reason
            if notify:
                await maybe_auto_update_status_message(app, job, force=True)

            if notify:
                await finish_job_card(app, job)
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass

    if run_in_background:
        asyncio.create_task(finish_download())
    else:
        await finish_download()

    return job


async def upload_and_delete_batch_artifact(
    app: Application,
    job: dict,
    filepath: str,
    item_index: int,
    total_items: int,
) -> bool:
    """Upload one completed batch artifact and remove it only after success."""
    full = Path(filepath).resolve()
    try:
        rel_path = file_rel_path(full)
    except ValueError as exc:
        raise RuntimeError(f"Downloaded file is outside the managed Download folder: {full}") from exc
    if not full.is_file():
        raise FileNotFoundError(str(full))

    chunks = split_file_into_chunks(rel_path)
    try:
        for chunk_path, chunk_index, chunk_total in chunks:
            if job.get("status") == "cancelled":
                return False
            chunk = Path(chunk_path).resolve()
            chunk_rel_path = file_rel_path(chunk)

            async def progress(current, total, *, part=chunk_index, parts=chunk_total):
                if job.get("status") == "cancelled":
                    raise StopTransmission()
                percent = (current / total * 100) if total else 0.0
                part_text = f", part {part}/{parts}" if parts > 1 else ""
                job["status"] = "uploading"
                job["last_line"] = (
                    f"Uploading {item_index}/{total_items}{part_text}: "
                    f"{full.name} ({percent:.1f}%)"
                )
                job["upload_length"] = int(current or 0)
                await maybe_auto_update_status_message(app, job)

            job["status"] = "uploading"
            job["last_line"] = f"Uploading {item_index}/{total_items}: {full.name}"
            await maybe_auto_update_status_message(app, job, force=True)
            try:
                await pyrogram_send_file(chunk_rel_path, progress_callback=progress)
            except StopTransmission:
                return False
    finally:
        cleanup_file_chunks(chunks)

    if job.get("status") == "cancelled":
        return False

    full.unlink()
    job["uploaded_items"] = int(job.get("uploaded_items", 0) or 0) + 1
    job["deleted_items"] = int(job.get("deleted_items", 0) or 0) + 1
    job["upload_length"] = 0
    return True


async def run_video_batch(
    app: Application,
    chat_id: int,
    urls: list[str] | tuple[str, ...],
    job: dict,
    *,
    item_label: str,
    user_id: int | None,
) -> None:
    """Run provider batch items sequentially using the job's snapshotted mode."""
    mode = normalize_batch_download_mode(job.get("batch_download_mode"))

    async def process_item(item_url: str, index: int, total_items: int) -> dict:
        child = await start_ytdlp_download(
            app,
            chat_id,
            item_url,
            audio_only=False,
            user_id=user_id,
            notify=False,
            parent_job=job,
        )
        if child.get("status") != "completed":
            reason = child.get("last_line") or child.get("status") or "Unknown error"
            raise RuntimeError(f"{item_label} {index} failed: {reason}")
        return child

    async def after_item(child: dict, index: int, total_items: int) -> None:
        filepath = str(child.get("filepath") or "")
        if mode is BatchDownloadMode.UPLOAD_AND_DELETE:
            if not filepath:
                raise RuntimeError(f"{item_label} {index} completed without an output file path.")
            deleted = await upload_and_delete_batch_artifact(
                app, job, filepath, index, total_items
            )
            if deleted:
                job["last_line"] = (
                    f"Uploaded and deleted {item_label.lower()} {index}/{total_items}"
                )
        else:
            job["last_line"] = f"Downloaded {item_label.lower()} {index}/{total_items}"
            if filepath:
                job.setdefault("outputs", []).append(filepath)

    async def on_progress(progress: BatchProgress) -> None:
        job["current_item"] = progress.current
        job["completed_items"] = progress.completed
        job["progress"] = (
            (progress.completed / progress.total * 100) if progress.total else 100.0
        )
        if progress.phase == "processing":
            job["status"] = "downloading"
            job["last_line"] = (
                f"Downloading {item_label.lower()} {progress.current}/{progress.total}"
            )
        await maybe_auto_update_status_message(app, job, force=True)

    await run_sequential_batch(
        urls,
        process_item,
        after_item=after_item,
        on_progress=on_progress,
        is_cancelled=lambda: job.get("status") == "cancelled",
    )


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
    batch_mode = normalize_batch_download_mode(
        get_user_settings(user_id or 0).get("batch_download_mode")
    )
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
        "uploaded_items": 0,
        "deleted_items": 0,
        "batch_download_mode": batch_mode.value,
        "folder": str(HENTAI_VIDEO_DIR),
    }
    download_jobs[job_id] = job

    async def run_playlist():
        try:
            job["status"] = "downloading"
            await maybe_auto_update_status_message(app, job, force=True)
            await run_video_batch(
                app,
                chat_id,
                playlist.urls,
                job,
                item_label="Episode",
                user_id=user_id,
            )
            if job.get("status") == "cancelled":
                return

            job["status"] = "completed"
            job["progress"] = 100.0
            job["finished_at"] = now_ts()
            job["last_line"] = "Completed"
            await maybe_auto_update_status_message(app, job, force=True)
            await finish_job_card(app, job)
        except Exception as exc:
            if job.get("status") == "cancelled":
                return
            logger.exception("Hentai playlist download failed")
            job["status"] = "failed"
            job["finished_at"] = now_ts()
            job["last_line"] = str(exc)
            await maybe_auto_update_status_message(app, job, force=True)
            await finish_job_card(app, job)

    asyncio.create_task(run_playlist())
    return job


async def start_pornhub_model_download(
    app: Application,
    chat_id: int,
    playlist: PornHubModelPlaylist,
    user_id: int = None,
):
    global job_counter, download_jobs

    async with jobs_lock:
        job_counter += 1
        job_id = job_counter

    video_count = len(playlist.urls)
    batch_mode = normalize_batch_download_mode(
        get_user_settings(user_id or 0).get("batch_download_mode")
    )
    job = {
        "id": job_id,
        "name": playlist.slug,
        "url": playlist.source_url,
        "chat_id": chat_id,
        "user_id": user_id or 0,
        "provider": "pornhub-model",
        "source_type": "adult_video_playlist",
        "platform": "PornHub",
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
        "last_line": "Preparing model videos...",
        "video_count": video_count,
        "completed_items": 0,
        "uploaded_items": 0,
        "deleted_items": 0,
        "batch_download_mode": batch_mode.value,
        "folder": str(ADULT_VIDEO_DIR / "PornHub"),
    }
    download_jobs[job_id] = job

    async def run_model_playlist():
        try:
            job["status"] = "downloading"
            await maybe_auto_update_status_message(app, job, force=True)
            await run_video_batch(
                app,
                chat_id,
                playlist.urls,
                job,
                item_label="Video",
                user_id=user_id,
            )
            if job.get("status") == "cancelled":
                return

            job["status"] = "completed"
            job["progress"] = 100.0
            job["finished_at"] = now_ts()
            job["last_line"] = "Completed"
            await maybe_auto_update_status_message(app, job, force=True)
            await finish_job_card(app, job)
        except Exception as exc:
            if job.get("status") == "cancelled":
                return
            logger.exception("PornHub model download failed")
            job["status"] = "failed"
            job["finished_at"] = now_ts()
            job["last_line"] = str(exc)
            await maybe_auto_update_status_message(app, job, force=True)
            await finish_job_card(app, job)

    asyncio.create_task(run_model_playlist())
    return job


@auto_answer
async def handle_link_request_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons on link prompts: lp:<request id>:<action>."""
    query = update.callback_query
    user_id = query.from_user.id
    _, rid, action = (query.data.split(":", 2) + ["", ""])[:3]
    request = link_requests.pop(rid, None)
    if request is None:
        await answer_once(
            query, "This prompt has expired or was already used. Send the link again.", show_alert=True
        )
        return
    await answer_once(query)
    app = context.application
    chat_id = request["chat_id"]
    kind = request["kind"]

    if action == "cancel":
        await safe_edit_message(query.message, f"{ICON_STOP} Cancelled. Nothing was downloaded.")
        return

    if kind == "video":
        audio_only = action == "mp3"
        max_height = int(action[1:]) if action.startswith("h") and action[1:].isdigit() else None
        job = await start_ytdlp_download(
            app, chat_id, request["url"], audio_only=audio_only, user_id=user_id,
            run_in_background=True, max_height=max_height, gif=action == "gif",
        )
    elif kind == "manga":
        job = await start_manga_download(app, chat_id, request["url"], user_id)
    elif kind == "spotify":
        job = await start_spotify_download(app, chat_id, request["url"], user_id)
    elif kind == "hentai":
        job = await start_hentai_playlist_download(app, chat_id, request["playlist"], user_id=user_id)
    elif kind == "pornhub":
        job = await start_pornhub_model_download(app, chat_id, request["playlist"], user_id=user_id)
    elif kind == "aria2":
        job = await start_aria2_download(app, chat_id, request["source"], user_id)
    elif kind == "gallery":
        job = await start_gallery_download(app, chat_id, request["url"], request["category"], user_id)
    else:
        return
    await attach_job_card(app, job, message=query.message)


@auto_answer
async def handle_stale_prompt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buttons from prompts sent by an older version of the bot."""
    await answer_once(
        update.callback_query,
        "This prompt is from an older version of the bot. Send the link again.",
        show_alert=True,
    )


async def _start_download_from_source(
    app: Application,
    chat_id: int,
    source: str,
    user_id: int = None,
):
    """Mini-app download entrypoint that chooses the right backend for pasted links."""
    source = source.strip()
    http_url = extract_http_url(source)
    if is_pornhub_model_url(http_url):
        playlist = await resolve_pornhub_model_playlist(
            http_url,
            cookies_file=cookies_file(),
            proxy=YTDLP_PROXY or None,
        )
        if not playlist.urls:
            raise RuntimeError("No public videos found on this PornHub model page.")
        return await start_pornhub_model_download(app, chat_id, playlist, user_id=user_id)
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



async def start_download_from_source(
    app: Application,
    chat_id: int,
    source: str,
    user_id: int = None,
):
    """Mini App entry point: start the right backend and post a job card in the chat."""
    job = await _start_download_from_source(app, chat_id, source, user_id)
    if isinstance(job, dict) and job.get("id"):
        await attach_job_card(app, job)
    return job

# =========================================================
# Background tasks
# =========================================================

# Strong references so running tasks are not garbage-collected mid-flight.
background_tasks: set[asyncio.Task] = set()


def run_in_background(coro, *, name: str, on_error=None) -> asyncio.Task:
    """Run ``coro`` without blocking the handler that started it.

    ``on_error(exc)`` is awaited if the task fails, so the user hears about
    it; failures are always logged.
    """

    async def runner():
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Background task %s failed", name)
            if on_error is not None:
                try:
                    await on_error(exc)
                except Exception:
                    logger.exception("Error reporter for %s failed", name)

    task = asyncio.create_task(runner(), name=name)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


def message_error_reporter(app: Application, chat_id: int, message_id: int, what: str):
    """on_error callback that turns a progress message into a failure notice."""

    async def report(exc: Exception) -> None:
        await _edit_dashboard_message(
            app,
            chat_id,
            message_id,
            f"{ICON_FAIL} {what} failed.\n\nReason:\n{shorten(str(exc), 600)}",
            InlineKeyboardMarkup([[InlineKeyboardButton(f"{ICON_FOLDER} Files", callback_data="fb:list:0:")]]),
        )

    return report


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


class PyrogramUnavailable(RuntimeError):
    """No Pyrogram user login is available; the bot runs in bot-only mode."""


def pyrogram_unavailable_reason() -> str | None:
    """Why the Pyrogram user client can't be used, or None when it can."""
    if not API_ID or not API_HASH:
        return "API_ID/API_HASH are not set"
    if PYRO_SESSION_STRING or (BASE_DIR / f"{PYRO_SESSION_NAME}.session").exists():
        return None
    if sys.stdin is not None and sys.stdin.isatty():
        return None  # first run in a terminal: Pyrogram asks for phone and code
    return (
        "there is no Pyrogram login yet (run `python main.py` once in a terminal to "
        "log in, or set PYRO_SESSION_STRING)"
    )


async def get_pyrogram_client():
    global pyro_client

    if pyro_client is None:
        reason = pyrogram_unavailable_reason()
        if reason:
            raise PyrogramUnavailable(reason)
        session_kwargs = (
            {"session_string": PYRO_SESSION_STRING, "in_memory": True}
            if PYRO_SESSION_STRING
            else {}
        )
        pyro_client = Client(
            PYRO_SESSION_NAME,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=str(BASE_DIR),
            **session_kwargs,
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


PYROGRAM_BOT_SESSION_HINT = (
    "Pyrogram is logged in as a bot.\n"
    f'Delete "{PYRO_SESSION_NAME}.session" and restart the script.\n'
    "Then log in with your personal Telegram account."
)


def _raise_if_bot_session(exc: RPCError) -> None:
    msg = str(exc)
    if "USER_IS_BOT" in msg or "A bot cannot send messages to other bots or to itself" in msg:
        raise RuntimeError(PYROGRAM_BOT_SESSION_HINT) from exc


def upload_progress_markup(upload_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ICON_STOP} Cancel upload", callback_data=f"up_cancel:{upload_id}")]
    ])


def upload_done_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ICON_FOLDER} Files", callback_data="fb:list:0:")]
    ])


def new_upload_job(chat_id: int, message_id: int, files: list) -> str:
    global upload_counter
    upload_counter += 1
    upload_id = f"upload_{upload_counter}"
    upload_jobs[upload_id] = {
        "id": upload_id,
        "status": "uploading",
        "files": list(files),
        "current_file": 0,
        "chat_id": chat_id,
        "message_id": message_id,
        "sent_count": 0,
        "last_update": 0,
        "cancelled": False,
    }
    return upload_id


def cancel_upload(upload_id: str) -> bool:
    job = upload_jobs.get(upload_id)
    if not job or job.get("status") != "uploading":
        return False
    job["cancelled"] = True
    return True


async def update_upload_progress(app: Application, chat_id: int, message_id: int, upload_id: str, file_label: str, sent: int, total: int):
    job = upload_jobs.get(upload_id)
    if job is None:
        return
    if job.get("cancelled"):
        # Raised inside Pyrogram's progress callback, this aborts the transfer
        # (the same thing Client.stop_transmission() does).
        raise StopTransmission()

    now = time.time()
    if now - job.get("last_update", 0) < 3.0 and sent < total:
        return
    job["last_update"] = now

    text = (
        f"{ICON_UPLOAD} Uploading to Saved Messages\n\n"
        f"{file_label}\n"
        f"{build_progress_bar(sent, total, width=15)}\n"
        f"{ICON_BOX} {human_size(sent)} / {human_size(total)}"
    )
    try:
        await _edit_dashboard_message(app, chat_id, message_id, text, upload_progress_markup(upload_id))
    except _DashboardMessageGone:
        logger.warning("Upload progress message %s no longer exists", message_id)
    except Exception as exc:
        logger.debug("Upload progress edit failed: %s", exc)


async def _send_via_bot_api(app: Application, job: dict, rel_path: str) -> None:
    """Bot-only mode: send a file of up to 50 MB to the chat through the Bot API."""
    full = safe_join(DOWNLOAD_DIR, rel_path)
    size = full.stat().st_size
    if size > BOT_MAX_DOCUMENT_BYTES:
        raise RuntimeError(
            f"{full.name} is {human_size(size)}. Without the Pyrogram login only files up to "
            "50 MB can be sent; set up the login to upload bigger files to Saved Messages."
        )
    with open(full, "rb") as fh:
        await app.bot.send_document(
            chat_id=job["chat_id"],
            document=fh,
            caption=build_upload_caption(rel_path),
            read_timeout=300,
            write_timeout=300,
        )
    job["via_bot"] = True


async def _upload_one_file(app: Application, upload_id: str, rel_path: str, label: str) -> bool:
    """Upload one file (split into parts above the size limit). False if cancelled."""
    job = upload_jobs[upload_id]
    try:
        await get_pyrogram_client()
    except PyrogramUnavailable:
        await _send_via_bot_api(app, job, rel_path)
        return True
    chunks = split_file_into_chunks(rel_path)
    try:
        for chunk_path, part, parts in chunks:
            if job["cancelled"]:
                return False
            part_label = f"{label} (part {part}/{parts})" if parts > 1 else label

            async def progress(current, total, part_label=part_label):
                await update_upload_progress(
                    app, job["chat_id"], job["message_id"], upload_id, part_label, current, total
                )

            try:
                await pyrogram_send_file(chunk_path, progress_callback=progress)
            except StopTransmission:
                return False
            except RPCError as exc:
                _raise_if_bot_session(exc)
                raise
            # Most send_* methods swallow StopTransmission and just return.
            if job["cancelled"]:
                return False
    finally:
        # Parts are a full second copy of the file on disk.
        cleanup_file_chunks(chunks)
    return True


async def upload_files_via_pyrogram(
    app: Application,
    chat_id: int,
    message_id: int,
    files: list,
    *,
    title: str,
    user_id: int = None,
) -> dict:
    """Upload ``files`` (paths relative to Download/) to Saved Messages.

    Progress is shown on ``message_id`` with a Cancel button; the message ends
    as a summary. Returns the upload job.
    """
    if not files:
        raise RuntimeError("No files to upload.")
    upload_id = new_upload_job(chat_id, message_id, files)
    job = upload_jobs[upload_id]
    total_files = len(files)
    try:
        for idx, rel in enumerate(files, start=1):
            if job["cancelled"]:
                break
            job["current_file"] = idx
            name = os.path.basename(rel)
            label = f"File {idx}/{total_files}: {name}" if total_files > 1 else name
            if not await _upload_one_file(app, upload_id, rel, label):
                break
            job["sent_count"] += 1
            await maybe_delete_file_after_upload(user_id, rel)
    except Exception:
        job["status"] = "failed"
        raise

    sent = job["sent_count"]
    if job["cancelled"]:
        job["status"] = "cancelled"
        text = (
            f"{ICON_STOP} Upload cancelled\n\n{title}\n"
            f"Uploaded before cancelling: {sent}/{total_files}"
        )
    else:
        job["status"] = "completed"
        destination = "this chat" if job.get("via_bot") else "Saved Messages"
        text = f"{ICON_OK} Upload complete\n\n{title}\nSent to: {destination}"
        if total_files > 1:
            text += f"\nFiles: {sent}/{total_files}"
    try:
        await _edit_dashboard_message(app, chat_id, message_id, text, upload_done_markup())
    except _DashboardMessageGone:
        await app.bot.send_message(chat_id=chat_id, text=text, reply_markup=upload_done_markup())
    return job


async def send_single_file_via_pyrogram(
    app: Application,
    chat_id: int,
    message_id: int,
    rel_path: str,
    user_id: int = None,
):
    return await upload_files_via_pyrogram(
        app, chat_id, message_id, [rel_path], title=f"Name: {os.path.basename(rel_path)}", user_id=user_id
    )


async def send_folder_files_via_pyrogram(
    app: Application,
    chat_id: int,
    message_id: int,
    rel_path: str,
    file_list: list = None,
    user_id: int = None,
):
    """Upload every file in a folder, or only ``file_list`` when given."""
    files = get_all_files_in_folder(rel_path) if file_list is None else file_list
    title = (
        f"Folder: /{rel_path.lstrip('/')}" if file_list is None else f"{len(files)} selected file(s)"
    )
    return await upload_files_via_pyrogram(app, chat_id, message_id, files, title=title, user_id=user_id)


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
JOB_ACTIVE_STATES = job_views.ACTIVE


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
    selected = [
        f.get("path")
        for f in status.get("files") or []
        if f.get("path") and str(f.get("selected", "true")) == "true"
    ]
    if selected:
        job["files"] = selected

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
        except Exception as exc:
            # A dropped RPC connection used to kill this task silently and leave
            # the job "downloading" forever. Retry for a while before giving up.
            job["monitor_errors"] = job.get("monitor_errors", 0) + 1
            logger.warning("aria2 monitor error for job %s: %s", job_id, exc)
            if job["monitor_errors"] >= 30:
                job["status"] = "failed"
                job["last_line"] = f"Lost contact with aria2: {exc}"
                break
            await asyncio.sleep(5)
            continue
        job["monitor_errors"] = 0

        if status.get("status") in ARIA2_DONE_STATES:
            break

        await asyncio.sleep(2)

    if job["status"] == "cancelled":
        job["finished_at"] = now_ts()
        await finish_job_card(app, job)
        return

    if job["status"] == "completed":
        job["status"] = "completed"
        logger.info(f"Download completed: {job['name']}")
        job["progress"] = 100.0
        job["finished_at"] = now_ts()
        await maybe_auto_update_status_message(app, job, force=True)

        await finish_job_card(app, job)

    else:
        job["status"] = "failed"
        logger.error(f"Download failed: {job['name']}")
        job["finished_at"] = now_ts()
        await maybe_auto_update_status_message(app, job, force=True)

        await finish_job_card(app, job)


async def aria2_add_source(source_spec: str) -> tuple[str, dict | None, str]:
    """Add a magnet/URL/.torrent (optionally ``path --select-file=1,2``) to aria2.

    If aria2 already has that torrent, return its existing download instead:
    (gid, status of the existing download or None, info hash).
    """
    source, selected_files = _split_torrent_source(source_spec)
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
    try:
        if _is_local_torrent_file(source):
            gid = await aria2_client.add_torrent(Path(source), options)
        else:
            gid = await aria2_client.add_uri(source, options)
        return gid, None, info_hash
    except Aria2RpcError as exc:
        duplicate_hash = extract_info_hash(str(exc)) or info_hash
        if "already registered" not in str(exc).lower() or not duplicate_hash:
            raise
        existing = await find_aria2_status_by_info_hash(duplicate_hash)
        if not existing:
            raise
        return existing["gid"], existing, duplicate_hash


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
    gid, initial_status, found_hash = await aria2_add_source(magnet)
    reattached = initial_status is not None
    info_hash = found_hash or info_hash

    async with jobs_lock:
        job_counter += 1
        job_id = job_counter

    job = {
        "id": job_id,
        "name": name,
        "source": magnet,
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
    if process is None and job.get("provider") in ("spotify", "gallery-dl"):
        # Still starting up: no subprocess yet, so stop the task that would start it.
        job["status"] = "cancelled"
        job["finished_at"] = now_ts()
        job["last_line"] = "Cancelled by user"
        task = job.get("task")
        if task is not None and not task.done():
            task.cancel()
        return True, f"Cancelled job #{job_id}: {job['name']}"
    if process is not None and job.get("provider") in ("spotify", "gallery-dl"):
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

    if job.get("provider") in {"manga", "yt-dlp", "hentai-playlist", "pornhub-model"}:
        # yt-dlp items (including the one a batch is on) see this status in
        # their progress hook and abort; manga downloads are cancelled directly.
        job["status"] = "cancelled"
        job["finished_at"] = now_ts()
        job["last_line"] = "Cancelled by user"
        task = job.get("task")
        if task is not None and not task.done():
            task.cancel()
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
    # Mutate in place: the Mini App and dashboard hold references to this dict.
    finished = [jid for jid, job in download_jobs.items() if job["status"] not in JOB_ACTIVE_STATES]
    for jid in finished:
        del download_jobs[jid]
    return len(finished)


# =========================================================
# Zipping Feature - Handlers
# =========================================================

async def zip_files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/zip: open the Archive screen."""
    context.user_data["archive_settings_back"] = "nav:archive"
    await show_screen(
        update.message, await build_archive_menu_screen(update.effective_user.id), edit=False
    )


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


def build_clear_jobs_prompt() -> tuple[str, InlineKeyboardMarkup | None]:
    finished = sum(1 for j in download_jobs.values() if j["status"] not in JOB_ACTIVE_STATES)
    if not finished:
        return f"{ICON_BROOM} No finished jobs to clear.", None
    return (
        f"{ICON_WARN} Clear {finished} finished job(s) from the list?\n\n"
        "Only the job list is cleared. Downloaded files are not touched.",
        InlineKeyboardMarkup([[
            InlineKeyboardButton(f"{ICON_BROOM} Yes, clear", callback_data="clear_confirm"),
            InlineKeyboardButton(f"{ICON_BACK} Cancel", callback_data="clear_cancel"),
        ]]),
    )


async def clear_jobs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/clear: remove finished jobs from the list, after confirmation."""
    text, markup = build_clear_jobs_prompt()
    await update.message.reply_text(text, reply_markup=markup)


# =========================================================
# Commands
# =========================================================

async def cancel_input_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel: stop whatever the bot is waiting for you to type or pick."""
    user_id = update.effective_user.id
    cancelled = []
    zip_session = zip_select_sessions.get(user_id)
    if zip_session and zip_session.pop("waiting_for", None):
        cancelled.append("archive password entry")
    if pending_zip_name_sessions.pop(user_id, None):
        cancelled.append("archive naming")
    if search_ui is not None and search_ui.is_waiting(context):
        search_ui.cancel_waiting(context)
        cancelled.append("torrent search")
    if torrent_select_sessions.pop(user_id, None):
        cancelled.append("torrent file selection")
    if context.user_data.pop("cookies_wait", None):
        cancelled.append("cookies upload")
    text = (
        f"{ICON_OK} Cancelled: {', '.join(cancelled)}."
        if cancelled
        else "Nothing to cancel. To stop a download, use Cancel on the 📊 Status card."
    )
    await update.message.reply_text(text, reply_markup=build_reply_menu(user_id))


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep links from the search results arrive as "/start <payload>".
    if context.args and search_ui is not None:
        if await search_ui.handle_start(update, context, context.args[0]):
            return
    await update.message.reply_text(
        home_views.welcome_text(),
        reply_markup=build_reply_menu(update.effective_user.id),
        parse_mode=ParseMode.HTML,
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_screen(update.message, build_home_screen(), edit=False)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_screen(update.message, home_views.help_screen(SUPPORTED_SITES_URL), edit=False)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_status_dashboard(
        context.application, update.effective_chat.id, update.effective_user.id
    )


async def supported_sites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_screen(update.message, home_views.sites_screen(SUPPORTED_SITES_URL), edit=False)


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_folder(update.message, "", 0, edit=False)


async def browse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open the modern file browser mini-app."""
    user_id = update.effective_user.id

    if not is_authorized_user(user_id):
        await update.message.reply_text("Unauthorized")
        return

    # Use inline keyboard with Web App button
    button = mini_app_inline_button("Open File Browser")
    if not button:
        return
    keyboard = InlineKeyboardMarkup([[button]])
    
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
    """/settings and /mangasettings: the settings hub."""
    await show_screen(update.message, build_settings_screen(update.effective_user.id), edit=False)


async def forwarded_posts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_authorized_user(user_id):
        await update.message.reply_text("⛔ Unauthorized")
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

async def build_archive_menu_screen(user_id: int):
    files = await asyncio.to_thread(lambda: filter_files_for_archiving(collect_download_files()))
    total = sum(path.stat().st_size for path in files if path.exists())
    return file_views.archive_menu_screen(
        len(files), total, settings_views.archive_summary(get_user_settings(user_id))
    )


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

    # Waiting for an archive name
    if user_id in pending_zip_name_sessions:
        safe_name = sanitize_archive_name(text)
        if not safe_name:
            await update.message.reply_text(
                "Please send a name made of letters or numbers (up to 100 characters), or /cancel."
            )
            return
        status_msg = await update.message.reply_text(f"📦 Creating {safe_name}...")
        run_in_background(
            run_named_zip(context.application, user_id, safe_name, status_msg),
            name="zip",
            on_error=message_error_reporter(context.application, chat_id, status_msg.message_id, "Archive"),
        )
        return

    # A link always wins over a pending search prompt; anything else typed
    # while the search prompt is open is the search query.
    is_link = lower.startswith("magnet:") or is_http_url(text)
    is_keyboard_button = text in KEYBOARD_LABELS
    if (is_link or is_keyboard_button) and search_ui is not None:
        search_ui.cancel_waiting(context)
    elif search_ui is not None and await search_ui.handle_text(update, context):
        return

    # normalize button text by removing emojis and extra spaces
    normalized = re.sub(r'[^\w\s\u0600-\u06FF]', '', lower).strip()

    try:
        if lower.startswith("magnet:"):
            name = extract_bt_name(text)
            if is_duplicate_name(name):
                rid = store_link_request("aria2", chat_id, source=text)
                await update.message.reply_text(
                    f"{ICON_WARN} A file named “{name}” is already in your downloads.",
                    reply_markup=link_request_markup(rid, [("📥 Download anyway", "go")]),
                )
                return

            job = await start_aria2_download(context.application, chat_id, text, user_id)
            await attach_job_card(context.application, job)

        elif is_manga_url(text):
            manga_url = extract_manga_url(text)
            rid = store_link_request("manga", chat_id, url=manga_url)
            await update.message.reply_text(
                build_manga_prompt_text(manga_url),
                reply_markup=link_request_markup(rid, [("📥 Download", "go")]),
                disable_web_page_preview=True,
            )

        elif is_spotify_url(text):
            spotify_url = extract_spotify_url(text)
            rid = store_link_request("spotify", chat_id, url=spotify_url)
            await update.message.reply_text(
                build_spotify_prompt_text(spotify_url),
                reply_markup=link_request_markup(rid, [("📥 Download", "go")]),
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

            rid = store_link_request("hentai", chat_id, playlist=playlist)
            await update.message.reply_text(
                build_hentai_playlist_prompt_text(playlist, user_id),
                reply_markup=link_request_markup(rid, [(f"📥 Download all {len(playlist.urls)}", "go")]),
                disable_web_page_preview=True,
            )

        elif is_pornhub_model_url(extract_http_url(text)):
            model_url = extract_http_url(text)
            try:
                playlist = await resolve_pornhub_model_playlist(
                    model_url,
                    cookies_file=cookies_file(),
                    proxy=YTDLP_PROXY or None,
                )
                if not playlist.urls:
                    raise RuntimeError("No public videos found on this PornHub model page.")
            except Exception as exc:
                await update.message.reply_text(
                    (
                        f"{ICON_FAIL} PornHub model detection failed.\n\n"
                        f"Reason:\n{shorten(str(exc), 900)}"
                    ),
                    reply_markup=build_reply_menu(user_id),
                    disable_web_page_preview=True,
                )
                return

            rid = store_link_request("pornhub", chat_id, playlist=playlist)
            await update.message.reply_text(
                build_pornhub_model_prompt_text(playlist, user_id),
                reply_markup=link_request_markup(rid, [(f"📥 Download all {len(playlist.urls)}", "go")]),
                disable_web_page_preview=True,
            )

        elif is_video_url(text):
            video_url = extract_http_url(text)
            platform = video_platform_label(video_url)
            choice = video_default_choice(user_id)
            if choice is not None:
                audio_only, max_height = choice
                job = await start_ytdlp_download(
                    context.application, chat_id, video_url, audio_only=audio_only,
                    user_id=user_id, run_in_background=True, max_height=max_height,
                )
                await attach_job_card(context.application, job)
            else:
                rid = store_link_request("video", chat_id, url=video_url)
                prompt = await update.message.reply_text(
                    f"🎬 {platform} link · checking available qualities…",
                    reply_markup=link_request_markup(rid, []),
                    disable_web_page_preview=True,
                )
                run_in_background(
                    show_quality_picker(prompt, rid, video_url, platform), name="quality-probe"
                )

        elif is_direct_http_download_url(text):
            direct_url = extract_http_url(text)
            name = extract_http_filename(direct_url)
            if is_duplicate_name(name):
                rid = store_link_request("aria2", chat_id, source=direct_url)
                await update.message.reply_text(
                    f"{ICON_WARN} A file named “{name}” is already in your downloads.",
                    reply_markup=link_request_markup(rid, [("📥 Download anyway", "go")]),
                )
                return

            job = await start_aria2_download(context.application, chat_id, direct_url, user_id)
            await attach_job_card(context.application, job)

        elif normalized in ("menu", "home", "main menu", "downloads", "download", "tools", "tool"):
            # "downloads"/"tools" are labels from the old keyboard.
            await show_screen(update.message, build_home_screen(), edit=False)

        elif normalized == "status":
            await show_status_dashboard(context.application, chat_id, user_id)

        elif normalized in ("settings", "setting", "manga settings", "manga"):
            await show_screen(update.message, build_settings_screen(user_id), edit=False)

        elif normalized == "queue":
            await update.message.reply_text(
                build_queue_text(user_id),
                reply_markup=build_reply_menu(user_id)
            )

        elif normalized in ("files", "file browser"):
            await show_folder(update.message, "", 0, edit=False)

        elif normalized == "cancel":
            active = [j for j in download_jobs.values() if j["status"] in JOB_ACTIVE_STATES]
            if not active:
                await update.message.reply_text(
                    f"{ICON_STOP} No active downloads to cancel.",
                    reply_markup=build_reply_menu(user_id)
                )
            else:
                await show_status_dashboard(context.application, chat_id, user_id)

        elif lower.startswith("cancel "):
            m = re.match(r"cancel\s+#?(\d+)", lower)
            jid = int(m.group(1)) if m else None
            if jid not in download_jobs:
                await update.message.reply_text(
                    "Usage: cancel <job number>, for example: cancel 3"
                    if jid is None
                    else f"Job #{jid} not found.",
                    reply_markup=build_reply_menu(user_id)
                )
                return
            job = download_jobs[jid]
            await update.message.reply_text(
                f"{ICON_WARN} Cancel job #{jid}?\n\n{shorten(clean_download_name(job['name']), 90)}\n"
                f"Status: {job['status']}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"{ICON_STOP} Yes, cancel it", callback_data=f"cancel_confirm:{jid}"),
                    InlineKeyboardButton("Keep it", callback_data="noop_close"),
                ]]),
            )

        elif normalized == "clear":
            await clear_jobs_cmd(update, context)

        elif normalized == "help":
            await show_screen(update.message, home_views.help_screen(SUPPORTED_SITES_URL), edit=False)

        elif normalized in ("zip menu", "zip", "archive"):
            context.user_data["archive_settings_back"] = "nav:archive"
            await show_screen(update.message, await build_archive_menu_screen(user_id), edit=False)

        elif normalized in ("search", "tpb search", "rarbg search", "rargb search", "prowlarr search"):
            provider_key = normalized.split()[0] if normalized != "search" else None
            provider_key = "rarbg" if provider_key == "rargb" else provider_key
            await search_ui.open(context, update.message, provider_key, edit=False)

        elif is_http_url(text) and (
            category := await asyncio.to_thread(gallery_category, extract_http_url(text))
        ):
            gallery_url = extract_http_url(text)
            rid = store_link_request("gallery", chat_id, url=gallery_url, category=category)
            await update.message.reply_text(
                f"🖼 <b>{html.escape(site_label(category))}</b> link\n\n"
                "Download it with gallery-dl into Download/Gallery?",
                reply_markup=link_request_markup(rid, [("📥 Download", "go")]),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        elif is_http_url(text):
            text_, markup = home_views.sites_screen(SUPPORTED_SITES_URL)
            await update.message.reply_text(
                f"{ICON_WARN} I don't know how to download this link.\n\n"
                "Supported: magnets, .torrent files, direct file links (ending in a file "
                "extension like .zip or .mkv), and the sites in the Supported sites list.",
                reply_markup=markup,
                disable_web_page_preview=True,
            )

        else:
            await update.message.reply_text(
                "I didn't understand that. Send a link, a magnet or a .torrent file to "
                "start a download, or use the keyboard below.",
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
    torrents_dir = DOWNLOAD_DIR / "_torrents"
    torrents_dir.mkdir(exist_ok=True)

    file = await doc.get_file()
    torrent_path = torrents_dir / Path(doc.file_name or f"upload_{doc.file_unique_id}.torrent").name
    await file.download_to_drive(str(torrent_path))

    return await start_torrent_file_selection(update, context, torrent_path, doc.file_name)


TORRENT_FILES_PER_PAGE = 8


async def start_torrent_file_selection(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    torrent_path: Path,
    title: str | None = None,
):
    files = await asyncio.to_thread(get_torrent_file_list, torrent_path)
    target = update.callback_query.message if update.callback_query else update.message

    if not files:
        await target.reply_text(f"{ICON_FAIL} Could not read the file list of this torrent.")
        return

    user_id = update.effective_user.id
    torrent_select_sessions[user_id] = {
        "torrent_path": str(torrent_path),
        "files": files,
        "selected": set(),
        "page": 0,
        "title": title or torrent_path.name,
    }
    await target.reply_text(
        build_torrent_select_text(user_id),
        reply_markup=build_torrent_select_keyboard(user_id, 0),
    )


def parse_aria2_show_files(output: str) -> list[dict]:
    """Parse ``aria2c --show-files`` output into [{"index", "path", "size"}].

    Each file is printed as ``  1|./path`` followed by ``   |230MiB (241,172,480)``.
    """
    files: list[dict] = []
    for line in output.splitlines():
        if "|" not in line:
            continue
        left, right = line.split("|", 1)
        left = left.strip()
        if left.isdigit():
            files.append({"index": left, "path": right.strip(), "size": ""})
        elif not left and files and not files[-1]["size"]:
            files[-1]["size"] = right.strip().split(" (")[0]
    return files


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
    return parse_aria2_show_files(result.stdout)


def build_torrent_select_text(user_id: int) -> str:
    session = torrent_select_sessions[user_id]
    total = len(session["files"])
    return (
        f"{ICON_MAGNET} Choose files to download\n\n"
        f"{shorten(clean_download_name(session['title']), 90)}\n"
        f"Selected: {len(session['selected'])} of {total} file(s)"
    )


def build_torrent_select_keyboard(user_id: int, page: int):
    session = torrent_select_sessions[user_id]
    files = session["files"]
    selected = session["selected"]
    pages = max(1, math.ceil(len(files) / TORRENT_FILES_PER_PAGE))
    page = max(0, min(page, pages - 1))
    session["page"] = page
    start = page * TORRENT_FILES_PER_PAGE

    rows = []
    for f in files[start:start + TORRENT_FILES_PER_PAGE]:
        checked = "✅" if f["index"] in selected else "⬜"
        size = f" · {f['size']}" if f.get("size") else ""
        rows.append([
            InlineKeyboardButton(
                f"{checked} {shorten(Path(f['path']).name, 34)}{size}",
                callback_data=f"tsel:{f['index']}",
            )
        ])

    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"tpage:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"tpage:{page + 1}"))
        rows.append(nav)

    rows.append([
        InlineKeyboardButton("☑️ Select all", callback_data="tselall"),
        InlineKeyboardButton("⬜ Clear", callback_data="tselnone"),
    ])
    if selected:
        rows.append([InlineKeyboardButton(f"✅ Download {len(selected)} selected", callback_data="tconfirm")])
    rows.append([InlineKeyboardButton("📦 Download everything", callback_data="tall")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="tcancel")])
    return InlineKeyboardMarkup(rows)


# =========================================================
# Callback handler
# =========================================================

@auto_answer
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    try:
        if data == "noop":
            pass

        elif data in ("nav:home", "menu_home", "menu:downloads", "menu:tools", "menu:files"):
            # The last three are buttons from menus that no longer exist.
            await show_screen(query.message, build_home_screen())

        elif data in ("nav:settings", "menu:settings", "menu:manga_settings"):
            await show_screen(query.message, build_settings_screen(user_id))

        elif data in ("nav:cookies", "ck:cancel"):
            context.user_data.pop("cookies_wait", None)
            await show_screen(query.message, build_cookies_screen(context))

        elif data == "ck:send":
            context.user_data["cookies_wait"] = time.time()
            await show_screen(query.message, build_cookies_screen(context))

        elif data == "ck:del":
            await show_screen(query.message, build_cookies_screen(context, confirm_delete=True))

        elif data == "ck:dely":
            UPLOADED_COOKIES_PATH.unlink(missing_ok=True)
            await answer_once(query, "Cookies removed.")
            await show_screen(query.message, build_cookies_screen(context))

        elif data in ("nav:archive_settings", "menu:zip_settings", "zip_menu:settings"):
            if data == "zip_menu:settings" and "archive_settings_back" not in context.user_data:
                context.user_data["archive_settings_back"] = "nav:archive"
            elif data != "zip_menu:settings":
                context.user_data["archive_settings_back"] = "nav:settings"
            await show_screen(query.message, build_archive_settings_screen(user_id, context))

        elif data in ("nav:archive", "menu:zip"):
            context.user_data["archive_settings_back"] = "nav:archive"
            pending_zip_name_sessions.pop(user_id, None)
            await show_screen(query.message, await build_archive_menu_screen(user_id))

        elif data == "nav:help":
            await show_screen(query.message, home_views.help_screen(SUPPORTED_SITES_URL))

        elif data in ("nav:status", "menu:status"):
            await show_status_dashboard(context.application, chat_id, user_id, replace=query.message)

        elif data.startswith("set:"):
            key = data.split(":", 1)[1]
            settings = get_user_settings(user_id)
            if key in settings_views.HUB_TOGGLES:
                await update_setting(user_id, key, not settings.get(key, False))
            elif key == "video_default":
                await update_setting(
                    user_id, key, settings_views.next_video_default(settings.get(key))
                )
            elif key == "batch_download_mode":
                current = normalize_batch_download_mode(settings.get(key))
                next_mode = (
                    BatchDownloadMode.DOWNLOAD_ONLY
                    if current is BatchDownloadMode.UPLOAD_AND_DELETE
                    else BatchDownloadMode.UPLOAD_AND_DELETE
                )
                await update_setting(user_id, key, next_mode.value)
            else:
                await answer_once(query, "This setting no longer exists.", show_alert=True)
                return
            await show_screen(query.message, build_settings_screen(user_id))

        elif data in ("menu:forwarded_posts", "menu:batch_mode", "menu:language") or data.startswith(
            "set_lang:"
        ):
            # Buttons from the old settings menu.
            if data == "menu:forwarded_posts":
                settings = get_user_settings(user_id)
                await update_setting(
                    user_id,
                    "auto_download_forwarded_posts",
                    not settings.get("auto_download_forwarded_posts"),
                )
            elif data.startswith(("menu:language", "set_lang:")):
                await answer_once(query, "The bot is English-only now.", show_alert=True)
            await show_screen(query.message, build_settings_screen(user_id))

        elif data.startswith("up_cancel:"):
            if cancel_upload(data.split(":", 1)[1]):
                await answer_once(query, "Cancelling upload...")
            else:
                await answer_once(query, "This upload already finished.", show_alert=True)

        elif data in ("refresh_status", "refresh_dashboard"):
            # Buttons from the old status and live-dashboard messages.
            await show_status_dashboard(context.application, chat_id, user_id, replace=query.message)

        elif data == "menu:file_browser":
            await show_folder(query.message, "", 0)

        elif data == "menu:clear":
            text, markup = build_clear_jobs_prompt()
            await safe_edit_message(query.message, text, markup)

        elif data in ("menu:tpb", "menu:rarbg", "menu:prowlarr", "menu:search"):
            # Buttons from older menus still in the chat history.
            provider_key = data.split(":", 1)[1]
            await search_ui.open(
                context, query.message, None if provider_key == "search" else provider_key, edit=True
            )

        elif data.startswith(("cancel_confirm:", "job_cancel:", "job_pause:", "job_resume:")):
            # cancel_confirm comes from the "cancel <n>" text command; the job_*
            # buttons from status messages sent before job cards existed.
            kind, _, raw_id = data.partition(":")
            jid = int(raw_id)
            action = {"job_pause": pause_job, "job_resume": resume_job}.get(kind, cancel_job)
            ok, msg = await action(jid)
            await answer_once(query, msg, show_alert=not ok)
            job = download_jobs.get(jid)
            if ok and job is not None:
                await refresh_job_card(context.application, job, force=True)
                if action is cancel_job:
                    job["card_final"] = job.get("status")
            if kind == "cancel_confirm":
                await safe_edit_message(query.message, f"{ICON_OK if ok else ICON_WARN} {msg}")
            else:
                await update_status_message(context.application, chat_id, user_id)

        elif data.startswith("manga_setting:"):
            # Buttons from the old manga settings screen.
            key = {
                "auto_convert": "manga_auto_convert_pdf",
                "remove_images": "manga_remove_images_after_conversion",
            }.get(data.split(":", 1)[1])
            if key:
                settings = get_user_settings(user_id)
                await update_setting(user_id, key, not settings.get(key, False))
            await show_screen(query.message, build_settings_screen(user_id))

        elif data == "noop_close":
            await safe_edit_message(query.message, "OK, nothing changed.")

        elif data == "clear_confirm":
            removed = clear_finished_jobs()
            await safe_edit_message(query.message, f"{ICON_OK} Cleared {removed} finished job(s).")

        elif data == "clear_cancel":
            await safe_edit_message(query.message, "Nothing was cleared.")
        
        # ===== Torrent Selection =====
        elif data in ("tconfirm", "tall", "tcancel", "tselall", "tselnone") or data.startswith(("tsel:", "tpage:")):
            session = torrent_select_sessions.get(user_id)
            if session is None:
                await answer_once(
                    query, "This file list has expired. Send the .torrent file again.", show_alert=True
                )
                return

            if data == "tcancel":
                torrent_select_sessions.pop(user_id, None)
                await safe_edit_message(query.message, "Torrent download cancelled.")
                return

            if data in ("tconfirm", "tall"):
                if data == "tconfirm" and not session["selected"]:
                    await answer_once(query, get_lang(user_id, 'select_at_least'), show_alert=True)
                    return
                torrent_select_sessions.pop(user_id, None)
                source = session["torrent_path"]
                count = len(session["files"])
                if data == "tconfirm":
                    selected = sorted(session["selected"], key=int)
                    source += f" --select-file={','.join(selected)}"
                    count = len(selected)
                job = await start_aria2_download(context.application, chat_id, source, user_id)
                job["note"] = f"{count} of {len(session['files'])} files selected"
                await attach_job_card(context.application, job, message=query.message)
                return

            if data.startswith("tsel:"):
                idx = data.split(":", 1)[1]
                session["selected"].symmetric_difference_update({idx})
            elif data == "tselall":
                session["selected"] = {f["index"] for f in session["files"]}
            elif data == "tselnone":
                session["selected"] = set()
            else:
                session["page"] = int(data.split(":", 1)[1])

            await safe_edit_message(
                query.message,
                build_torrent_select_text(user_id),
                build_torrent_select_keyboard(user_id, session["page"]),
            )

        # ===== File Browser =====
        elif data.startswith("fb:"):
            parts = data.split(":", 3)
            action = parts[1]
            # Buttons from the previous file browser.
            action = {
                "dir": "list",
                "dirinfo": "more",
                "send_confirm": "send_yes",
                "upload_file_confirm": "send_yes",
                "upload_file_yes": "send_yes",
                "delete_file_confirm": "delete_confirm",
                "delete_file_yes": "delete_yes",
                "batch": "sel",
                "batchdel": "sel",
                "bselect": "st",
                "blist": "sp",
                "bupload": "sup",
                "bdelete_confirm": "sdel",
                "bdelete_yes": "sdelyes",
            }.get(action, action)
            page_raw = parts[2] if len(parts) > 2 else ""
            page = int(page_raw) if page_raw.lstrip("-").isdigit() else 0
            encoded = parts[3] if len(parts) > 3 else ""

            if action in ("st", "sp", "sall", "snone", "sup", "szip", "sdel", "sdelyes", "sdone", "bupload_empty"):
                await handle_file_selection(update, context, action, parts)
                return

            if action in ("list", "o", "file", "more", "send_yes", "send_folder_confirm", "send_folder_yes",
                          "delete_confirm", "delete_yes", "deleteall_confirm", "deleteall_yes", "zipdir",
                          "sel", "manga_pdf", "conv_menu", "thumb_send"):
                rel_path = decode_path(encoded.split(":", 1)[0]) if encoded else ""

            if action == "list":
                await show_folder(query.message, rel_path, page)

            elif action == "o":
                full = safe_join(DOWNLOAD_DIR, rel_path)
                if full.is_dir():
                    await show_folder(query.message, rel_path, 0)
                elif full.is_file():
                    await show_file(query.message, rel_path, page)
                else:
                    raise FileNotFoundError(rel_path)

            elif action == "file":
                await show_file(query.message, rel_path, page)

            elif action == "more":
                info = await asyncio.to_thread(folder_info, rel_path)
                await show_screen(query.message, file_views.more_screen(
                    rel_path, info, page, encode_path,
                    is_manga=is_manga_gallery_folder(safe_join(DOWNLOAD_DIR, rel_path)),
                ))

            elif action == "send_yes":
                if not safe_join(DOWNLOAD_DIR, rel_path).is_file():
                    raise FileNotFoundError(rel_path)
                await answer_once(query)
                await safe_edit_message(query.message, f"{ICON_UPLOAD} Preparing upload...")
                run_in_background(
                    send_single_file_via_pyrogram(
                        context.application, query.message.chat_id, query.message.message_id,
                        rel_path, user_id=user_id,
                    ),
                    name="upload",
                    on_error=message_error_reporter(
                        context.application, query.message.chat_id, query.message.message_id, "Upload"
                    ),
                )

            elif action == "send_folder_confirm":
                info = await asyncio.to_thread(folder_info, rel_path)
                if not info["file_count"]:
                    await answer_once(query, "There are no files in this folder.", show_alert=True)
                    return
                await safe_edit_message(
                    query.message,
                    f"{ICON_UPLOAD} Upload {info['file_count']} file(s) "
                    f"({human_size(info['total_size'])}) from /{rel_path} to Saved Messages?",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{ICON_OK} Yes, upload all", callback_data=f"fb:send_folder_yes:{page}:{encoded}")],
                        [InlineKeyboardButton("✖ Cancel", callback_data=f"fb:more:{page}:{encoded}")],
                    ]),
                )

            elif action == "send_folder_yes":
                await answer_once(query)
                await safe_edit_message(query.message, f"{ICON_UPLOAD} Preparing folder upload...")
                run_in_background(
                    send_folder_files_via_pyrogram(
                        context.application, query.message.chat_id, query.message.message_id,
                        rel_path, user_id=user_id,
                    ),
                    name="upload",
                    on_error=message_error_reporter(
                        context.application, query.message.chat_id, query.message.message_id, "Upload"
                    ),
                )

            elif action == "delete_confirm":
                full = safe_join(DOWNLOAD_DIR, rel_path)
                if not rel_path or not full.exists():
                    raise FileNotFoundError(rel_path)
                info = await asyncio.to_thread(folder_info if full.is_dir() else file_info, rel_path)
                await show_screen(query.message, file_views.delete_confirm_screen(
                    rel_path, full.is_dir(), info, page, encode_path
                ))

            elif action == "delete_yes":
                if not rel_path:
                    raise ValueError("Refusing to delete the download root")
                kind = await asyncio.to_thread(delete_path, rel_path)
                await answer_once(query, f"Deleted {kind}: {rel_name(rel_path)}")
                await show_folder(query.message, rel_parent(rel_path), page)

            elif action == "deleteall_confirm":
                entries = await asyncio.to_thread(list_dir, rel_path)
                if not entries:
                    await answer_once(query, "This folder is already empty.", show_alert=True)
                    return
                info = await asyncio.to_thread(folder_info, rel_path)
                await safe_edit_message(
                    query.message,
                    f"{ICON_WARN} Permanently delete everything inside /{rel_path}?\n\n"
                    f"{info['file_count']} files in {info['folder_count']} folders · "
                    f"{human_size(info['total_size'])}\n\nThis cannot be undone.",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton("🗑 Yes, delete everything", callback_data=f"fb:deleteall_yes:{page}:{encoded}")],
                        [InlineKeyboardButton("✖ Cancel", callback_data=f"fb:more:{page}:{encoded}")],
                    ]),
                )

            elif action == "deleteall_yes":
                files_n, folders_n, errors = await asyncio.to_thread(delete_all_in_directory, rel_path)
                text = f"Deleted {files_n} file(s) and {folders_n} folder(s)."
                if errors:
                    text += f" {len(errors)} item(s) could not be deleted."
                await answer_once(query, text, show_alert=bool(errors))
                await show_folder(query.message, rel_path, 0)

            elif action == "zipdir":
                files = await asyncio.to_thread(get_all_files_in_folder, rel_path)
                paths = filter_files_for_archiving([safe_join(DOWNLOAD_DIR, f) for f in files])
                if not paths:
                    await answer_once(query, "There are no files to zip here.", show_alert=True)
                    return
                await ask_zip_name(
                    query.message, user_id, chat_id, paths,
                    default_name=rel_name(rel_path) if rel_path else default_archive_name(),
                    cancel_data=f"fb:more:{page}:{encoded}",
                )

            elif action == "sel":
                batch_select_sessions[user_id] = {"rel_path": rel_path, "selected": set(), "page": 0}
                await show_selection(query.message, user_id)

            elif action == "manga_pdf":
                folder = safe_join(DOWNLOAD_DIR, rel_path)
                if not is_manga_gallery_folder(folder):
                    await answer_once(query, "No manga images found in this folder.", show_alert=True)
                    return
                await answer_once(query)
                await safe_edit_message(query.message, f"{ICON_IMAGE} Converting images to PDF...\n\n{folder.name}")
                pdf_path = await convert_manga_folder_to_pdf_job(folder, user_id)
                pdf_rel = file_rel_path(pdf_path)
                await safe_edit_message(
                    query.message,
                    f"{ICON_OK} PDF created\n\n{pdf_path.name}",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(f"{ICON_UPLOAD} Upload the PDF", callback_data=f"fb:send_yes:0:{encode_path(pdf_rel)}")],
                        [InlineKeyboardButton(f"{ICON_FOLDER} Open its folder", callback_data=f"fb:list:0:{encode_path(rel_parent(pdf_rel))}")],
                    ]),
                )

            elif action == "conv_menu":
                await safe_edit_message(
                    query.message,
                    f"🎬 Convert {rel_name(rel_path)} to:",
                    InlineKeyboardMarkup([
                        [InlineKeyboardButton(res, callback_data=f"fb:conv_start:{page}:{encoded}:{res}")
                         for res in ("1080p", "720p")],
                        [InlineKeyboardButton(res, callback_data=f"fb:conv_start:{page}:{encoded}:{res}")
                         for res in ("480p", "360p")],
                        [InlineKeyboardButton("✖ Cancel", callback_data=f"fb:file:{page}:{encoded}")],
                    ]),
                )

            elif action == "thumb_send":
                await answer_once(query, "Making thumbnails…")
                await send_thumbnail(update, context, rel_path)

            elif action == "organize":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = decode_path(encoded) if encoded else ""
                plan = await asyncio.to_thread(build_organize_plan, rel_path, active_job_names())
                text, markup = build_organize_preview(rel_path, page, plan)
                await safe_edit_message(query.message, text, markup)

            elif action == "organize_go":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded, _, mode = (parts[3] if len(parts) > 3 else "").partition(":")
                rel_path = decode_path(encoded) if encoded else ""
                await safe_edit_message(query.message, f"{ICON_BROOM} Organizing videos...")
                # Re-plan so files that changed since the preview are handled correctly.
                plan = await asyncio.to_thread(build_organize_plan, rel_path, active_job_names())
                result = await asyncio.to_thread(
                    apply_organize, plan, delete_leftovers=(mode == "purge")
                )
                lines = [
                    f"{ICON_OK} Organized videos",
                    "",
                    f"Moved: {result.moved} video(s)",
                    f"Folders removed: {result.removed_folders}",
                ]
                if result.kept_folders:
                    lines.append(f"Folders kept (other files inside): {result.kept_folders}")
                if plan.skipped_busy:
                    lines.append(f"Skipped (download in progress): {len(plan.skipped_busy)}")
                if result.errors:
                    lines.append("")
                    lines.append(f"{ICON_WARN} Problems:")
                    lines.extend(f"• {shorten(err, 80)}" for err in result.errors[:5])
                await safe_edit_message(
                    query.message,
                    "\n".join(lines),
                    InlineKeyboardMarkup([[InlineKeyboardButton(
                        f"{ICON_FOLDER} Open folder", callback_data=f"fb:list:{page}:{encoded}"
                    )]]),
                )

            elif action == "conv_start":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                # data.split(":", 3) keeps "<token>:<resolution>" together in parts[3].
                encoded, _, target_res = (parts[3] if len(parts) > 3 else "").partition(":")
                force = target_res.endswith("!")
                target_res = target_res.rstrip("!") or "720p"
                rel_path = decode_path(encoded) if encoded else ""
                input_path = safe_join(DOWNLOAD_DIR, rel_path)
                if not input_path.exists() or not is_video_file(str(input_path)):
                    await answer_once(query, "This is not a video file (any more).", show_alert=True)
                    return
                await answer_once(query)

                output_path = input_path.parent / f"{input_path.stem}_{target_res}{input_path.suffix}"
                back_to_file = InlineKeyboardButton("🔙 Back to file", callback_data=f"fb:file:{page}:{encoded}")
                if output_path.exists() and not force:
                    await safe_edit_message(
                        query.message,
                        f"⏩ A {target_res} version already exists:\n{output_path.name}",
                        InlineKeyboardMarkup([
                            [InlineKeyboardButton("📤 Upload it", callback_data=f"fb:conv_upload:{page}:{encoded}:{encode_path(file_rel_path(output_path))}")],
                            [InlineKeyboardButton("🔁 Convert again (replace it)", callback_data=f"fb:conv_start:{page}:{encoded}:{target_res}!")],
                            [back_to_file],
                        ]),
                    )
                    return
                if force:
                    output_path.unlink(missing_ok=True)

                msg = query.message
                await safe_edit_message(msg, f"🔄 Converting to {target_res}... 0%")

                async def run_conversion():
                    async def progress_callback(pct):
                        try:
                            await msg.edit_text(f"🔄 Converting to {target_res}... {pct}%")
                        except Exception as exc:
                            logger.debug("Conversion progress edit failed: %s", exc)

                    await convert_video_quality(
                        str(input_path), str(output_path), target_res, progress_callback
                    )
                    await msg.edit_text(
                        f"✅ Conversion finished!\nOutput: {output_path.name}\nSize: {human_size(output_path.stat().st_size)}",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("📤 Upload converted file", callback_data=f"fb:conv_upload:{page}:{encoded}:{encode_path(file_rel_path(output_path))}")],
                            [back_to_file],
                        ]),
                    )

                run_in_background(
                    run_conversion(),
                    name="convert",
                    on_error=message_error_reporter(
                        context.application, msg.chat_id, msg.message_id, "Conversion"
                    ),
                )

            elif action == "conv_upload":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                original_encoded, _, converted_encoded = (parts[3] if len(parts) > 3 else "").partition(":")
                converted_rel = decode_path(converted_encoded) if converted_encoded else ""
                await safe_edit_message(query.message, f"{ICON_UPLOAD} Uploading converted file...")
                run_in_background(
                    send_single_file_via_pyrogram(
                        context.application,
                        query.message.chat_id,
                        query.message.message_id,
                        converted_rel,
                        user_id=user_id,
                    ),
                    name="upload",
                    on_error=message_error_reporter(
                        context.application, query.message.chat_id, query.message.message_id, "Upload"
                    ),
                )

            # New: Send thumbnail (multiple frames)


        # ===== Archive =====
        elif data == "zipname:default":
            session = pending_zip_name_sessions.get(user_id)
            if session is None:
                await answer_once(query, "This archive request has expired.", show_alert=True)
                return
            await answer_once(query)
            run_in_background(
                run_named_zip(context.application, user_id, session["default_name"], query.message),
                name="zip",
                on_error=message_error_reporter(
                    context.application, chat_id, query.message.message_id, "Archive"
                ),
            )

        elif data in ("zip_menu:zip_all",):
            all_files = await asyncio.to_thread(lambda: filter_files_for_archiving(collect_download_files()))
            if not all_files:
                await answer_once(query, "There are no files to zip.", show_alert=True)
                return
            await ask_zip_name(
                query.message, user_id, chat_id, all_files,
                default_name=default_archive_name(), cancel_data="nav:archive",
            )

        elif data.startswith(("zip_menu:", "zip_select:")):
            # zip_menu:back/list/select and zip_select:* come from the old archive menu.
            if data.startswith(("zip_menu:select", "zip_select:")):
                batch_select_sessions[user_id] = {"rel_path": "", "selected": set(), "page": 0}
                await show_selection(query.message, user_id)
            else:
                await show_screen(query.message, await build_archive_menu_screen(user_id))

        # ===== Zip Settings =====
        elif data.startswith("zip_setting:"):
            setting_key = data.split(":")[1]
            back = "nav:archive_settings"

            if setting_key == "part_size":
                await show_screen(query.message, settings_views.archive_choice_screen(
                    "Split archives into parts of",
                    [
                        ("256 MB", "zip_set_value:zip_part_size:268435456"),
                        ("512 MB", "zip_set_value:zip_part_size:536870912"),
                        ("1 GB", "zip_set_value:zip_part_size:1073741824"),
                        ("2 GB (largest Telegram upload)", "zip_set_value:zip_part_size:2147483648"),
                    ],
                    back,
                ))

            elif setting_key == "method":
                await show_screen(query.message, settings_views.archive_choice_screen(
                    "Archive format",
                    [
                        ("ZIP — opens everywhere", "zip_set_value:zip_method:zip"),
                        ("7Z — smaller, needs 7-Zip", "zip_set_value:zip_method:7z"),
                    ],
                    back,
                ))

            elif setting_key == "compression":
                await show_screen(query.message, settings_views.archive_choice_screen(
                    "Compression level (1 = fastest, 9 = smallest)",
                    [(str(level), f"zip_set_value:compression_level:{level}") for level in range(1, 10)],
                    back,
                ))

            elif setting_key == "password":
                session = zip_select_sessions.get(user_id, {"selected": set(), "page": 0})
                session["waiting_for"] = "password"
                zip_select_sessions[user_id] = session
                await safe_edit_message(
                    query.message,
                    "🔐 Send the archive password as your next message.\n\n"
                    "Send none to remove the password, or /cancel to keep the current one.",
                )

            elif setting_key in ("auto_del_files", "auto_del_zips", "auto_del_upload", "forwarded_posts"):
                setting_name = {
                    "auto_del_files": "auto_delete_files_after_zip",
                    "auto_del_zips": "auto_delete_zips_after_send",
                    "auto_del_upload": "auto_delete_files_after_upload",
                    "forwarded_posts": "auto_download_forwarded_posts",
                }[setting_key]
                settings = get_user_settings(user_id)
                await update_setting(user_id, setting_name, not settings.get(setting_name, False))
                await show_screen(query.message, build_archive_settings_screen(user_id, context))

            else:
                # e.g. batch_mode from the old screen; it lives in Settings now.
                await show_screen(query.message, build_settings_screen(user_id))

        elif data.startswith("zip_set_value:"):
            parts = data.split(":")
            setting_name = parts[1]
            value = ":".join(parts[2:])

            if setting_name in ("zip_part_size", "compression_level"):
                try:
                    value = int(value)
                except ValueError:
                    # FIX #7: Use translated string instead of hardcoded
                    await answer_once(query, get_lang(user_id, 'invalid_value'), show_alert=True)
                    return
            elif setting_name == "zip_method":
                value = value.lower()
                if value not in ("zip", "7z"):
                    await answer_once(query, get_lang(user_id, 'invalid_archive_method'), show_alert=True)
                    return
                fmt_err = check_archive_format_support(value)
                if fmt_err:
                    await answer_once(query, fmt_err, show_alert=True)
                    return
            elif setting_name in (
                "auto_delete_files_after_zip",
                "auto_delete_zips_after_send",
                "auto_delete_files_after_upload",
            ):
                value = value.lower() == "true"

            if setting_name == "zip_part_size":
                if not validate_part_size(value // (1024 * 1024)):
                    await answer_once(query, get_lang(user_id, 'part_size_error'), show_alert=True)
                    return
            elif setting_name == "compression_level":
                if not validate_compression_level(value):
                    # FIX #7: Use translated string instead of hardcoded
                    await answer_once(query, get_lang(user_id, 'compression_error'), show_alert=True)
                    return

            ok = await update_setting(user_id, setting_name, value)
            if not ok:
                await answer_once(query, get_lang(user_id, 'invalid_value'), show_alert=True)
                return

            await show_screen(query.message, build_archive_settings_screen(user_id, context))

    except Exception as e:
        logger.exception("Button %r failed", data)
        if isinstance(e, (ValueError, FileNotFoundError, NotADirectoryError, IsADirectoryError)):
            # Usually an expired path token or a file deleted since the menu was drawn.
            text = "That item is gone or this menu is too old. Open Files again."
        else:
            text = f"Something went wrong: {shorten(str(e), 150)}"
        # Prefer a pop-up so the current screen stays usable.
        if not await answer_once(query, text, show_alert=True):
            await safe_edit_message(
                query.message,
                f"{ICON_WARN} {text}",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"{ICON_FOLDER} Files", callback_data="fb:list:0:")],
                ]),
            )



# =========================================================
# Main
# =========================================================

def save_jobs_now() -> None:
    if job_store is not None:
        job_store.sync([dict(job) for job in download_jobs.values()])


async def job_persistence_loop():
    last_fingerprint = None
    while True:
        await asyncio.sleep(JOB_SAVE_INTERVAL)
        try:
            snapshot = [dict(job) for job in download_jobs.values()]
            # Skip the write (and its fsync) when nothing changed since the last save.
            fingerprint = _hash_content(repr([sorted(map(str, job.items())) for job in snapshot]))
            if fingerprint == last_fingerprint:
                continue
            await asyncio.to_thread(job_store.sync, snapshot)
            last_fingerprint = fingerprint
        except Exception:
            logger.exception("Saving jobs failed")


async def reattach_aria2_job(app: Application, job: dict) -> None:
    """Pick a torrent/direct download back up after the bot restarted.

    aria2 keeps its own session file, so the download is usually still there
    under the same GID; otherwise it is found by info hash or added again
    (aria2 resumes from the partial files on disk).
    """
    try:
        await aria2_client.ensure_started()
        status = None
        for gid in dict.fromkeys([job.get("gid"), *reversed(job.get("gid_history") or [])]):
            if not gid:
                continue
            try:
                status = await aria2_client.tell_status(gid)
                break
            except Aria2RpcError:
                continue
        if status is None and job.get("info_hash"):
            status = await find_aria2_status_by_info_hash(job["info_hash"])
        if status is not None:
            job["gid"] = status.get("gid", job.get("gid"))
        elif job.get("source"):
            job["gid"], _, _ = await aria2_add_source(job["source"])
        else:
            raise RuntimeError("aria2 no longer knows this download")
        job["monitor_errors"] = 0
        job["note"] = "resumed after a restart"
        run_in_background(monitor_aria2_job(app, job["id"]), name=f"aria2-{job['id']}")
    except Exception as exc:
        logger.warning("Could not reattach job %s: %s", job.get("id"), exc)
        job["status"] = "failed"
        job["last_line"] = f"Could not resume after the bot restarted: {exc}"
        job["finished_at"] = now_ts()
        await finish_job_card(app, job)


async def restore_jobs(app: Application) -> None:
    """Load saved jobs: keep finished ones, resume aria2 ones, mark the rest interrupted."""
    global job_counter
    try:
        saved = await asyncio.to_thread(job_store.load)
    except Exception:
        logger.exception("Loading saved jobs failed")
        return
    for job in saved:
        download_jobs[job["id"]] = job
        job_counter = max(job_counter, int(job["id"]))
    for job in saved:
        if job.get("status") not in JOB_ACTIVE_STATES:
            continue
        if not job.get("provider") and (job.get("gid") or job.get("source")):
            await reattach_aria2_job(app, job)
        else:
            # yt-dlp/Spotify/manga run inside the bot process and stopped with it.
            job["status"] = "failed"
            job["last_line"] = "Interrupted by a bot restart. Tap Retry to resume it."
            job["finished_at"] = now_ts()
            await finish_job_card(app, job)
    if saved:
        logger.info("Restored %d saved job(s)", len(saved))


async def post_init(app: Application):
    """Initialize bot - setup pyrogram, executor, and start auto-cleanup task."""
    global zip_executor

    if search_ui is not None:
        search_ui.bot_username = app.bot.username  # result links are t.me/<bot>?start=…
    
    # Initialize thread pool executor for zip operations
    # Using ThreadPoolExecutor instead of ProcessPoolExecutor to properly share
    # the progress object across threads. With GIL, this is safe for I/O-bound work.
    # Limit to 2 workers to prevent bot overload from concurrent zip operations
    zip_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="zip_worker")
    
    # FIX #10: Verify pyrogram is logged in as user account (not bot)
    try:
        client = await get_pyrogram_client()
        me = await client.get_me()
        if me.is_bot:
            raise RuntimeError(
                f"❌ FATAL: Pyrogram is logged in as a BOT (@{me.username}).\n"
                f"You must log in with a PERSONAL USER account instead.\n"
                f"Fix: Delete '{PYRO_SESSION_NAME}.session' and restart the script.\n"
                f"Then log in with your personal Telegram account."
            )
        logger.info(f"✅ Pyrogram logged in as user: @{me.username}")
    except PyrogramUnavailable as exc:
        logger.warning(
            "Running in bot-only mode because %s. Files up to 50 MB are sent through the "
            "Bot API; bigger uploads and forwarded-media capture need the Pyrogram login.",
            exc,
        )
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

    global job_store
    try:
        job_store = JobStore(JOB_DB_PATH)
        await restore_jobs(app)
        run_in_background(job_persistence_loop(), name="job-persistence")
    except Exception:
        logger.exception("Job persistence unavailable; jobs will not survive restarts")
        job_store = None

    # The "/" menu Telegram shows next to the message box.
    try:
        await app.bot.set_my_commands(
            [BotCommand(name, description) for name, description in home_views.COMMANDS]
        )
    except Exception as exc:
        logger.warning("Unable to register bot commands: %s", exc)


    if WEB_APP_ENABLE and WEB_APP_URL:
        try:
            if WEB_APP_URL.lower().startswith("https://"):
                await app.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="Mini-App",
                        web_app=WebAppInfo(url=WEB_APP_URL),
                    )
                )
                logger.info("Telegram mini-app menu button configured")
            else:
                logger.info(
                    "WEB_APP_URL is not HTTPS (%s); skipping native mini-app menu "
                    "button. Telegram only opens WebApp buttons over HTTPS. Use "
                    "scripts/start_with_tunnel.py for a free HTTPS URL, or open "
                    "the dashboard via the browser button.",
                    WEB_APP_URL,
                )
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
    if job_store is not None:
        try:
            save_jobs_now()
            job_store.close()
        except Exception:
            logger.exception("Final job save failed")
    await stop_pyrogram_client()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning(f"Telegram network error: {err}")
        return
    logger.error(f"Telegram error: {err}", exc_info=err)


async def start_search_download(update, context, source: str, force: bool = False) -> dict:
    """Start an aria2 download for a torrent-search result.

    Returns {"status": "duplicate", "name": ...} instead of starting when a
    file of that name already exists, unless ``force`` is set.
    """
    source_value, _ = _split_torrent_source(source)
    if _is_local_torrent_file(source_value):
        name = clean_download_name(Path(source_value).name)
    elif source_value.lower().startswith(("http://", "https://")):
        name = extract_http_filename(source_value)
    else:
        name = extract_bt_name(source_value)
    if not force and is_duplicate_name(name):
        return {"id": 0, "name": name, "status": "duplicate"}
    user_id = update.effective_user.id if update.effective_user else None
    job = await start_aria2_download(context.application, update.effective_chat.id, source, user_id)
    await attach_job_card(context.application, job)
    return job


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")
    if not API_ID or not API_HASH:
        logger.warning("API_ID/API_HASH missing: uploads over 50 MB will not be available")
    if not ALLOWED_USER_IDS:
        raise RuntimeError(
            "ALLOWED_USER_IDS is empty. Set it in .env to your numeric Telegram user ID "
            "(message @userinfobot to find it). The bot refuses to start without it "
            "because anyone could otherwise control your server's downloads and files."
        )

    # Configure HTTP request with proper timeouts
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    async def _wrapped_post_init(app_ref: Application):
        await post_init(app_ref)

        async def notify_forwarded(user_id: int, text: str, message_id: int | None):
            """Send (or edit) forwarded-media status as the bot, in the owner's bot chat."""
            try:
                if message_id is None:
                    msg = await app_ref.bot.send_message(chat_id=user_id, text=text)
                    return msg.message_id
                await app_ref.bot.edit_message_text(
                    chat_id=user_id, message_id=message_id, text=text
                )
            except Exception as exc:
                logger.warning("Forwarded-media status message failed: %s", exc)
            return message_id

        # Start Pyrogram eagerly and register forwarded-media handler
        try:
            client = await get_pyrogram_client()
            setup_pyrogram_forwarded_downloads(
                client,
                str(TELEGRAM_DIR),
                bot_id=bot_id_from_token(BOT_TOKEN),
                notify=notify_forwarded,
            )
        except Exception as exc:
            logger.warning("Pyrogram forwarded-media handler not registered: %s", exc)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(_wrapped_post_init)
        .post_shutdown(post_shutdown)
        # Handle updates concurrently so one slow action (a big search, a
        # conversion) never freezes the rest of the bot.
        .concurrent_updates(True)
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

    # Runs before every other handler and stops updates from unknown users.
    app.add_handler(TypeHandler(Update, authorization_gate), group=-1)

    # Core commands
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_input_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("sites", supported_sites_cmd))
    app.add_handler(CommandHandler("files", files_cmd))
    app.add_handler(CommandHandler("browse", browse_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("mangasettings", settings_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("forwardedposts", forwarded_posts_cmd))
    app.add_handler(CommandHandler("autoforward", forwarded_posts_cmd))

    # Zipping feature handlers
    app.add_handler(CommandHandler("zip", zip_files_cmd))
    app.add_handler(CommandHandler("list", list_files_cmd))
    app.add_handler(CommandHandler("clear", clear_jobs_cmd))

    # Unified torrent search (Prowlarr, TPB, RARBG-style mirror)
    global search_ui
    search_ui = SearchUI(
        [
            ProwlarrProvider(
                ProwlarrClient(PROWLARR_URL, PROWLARR_API_KEY, PROWLARR_SEARCH_LIMIT)
            ),
            TPBProvider(TPBCrawler(TPB_API_URL)),
            RARBGProvider(RARBGCrawler(RARBG_BASE_URL)),
        ],
        start_download=start_search_download,
        select_files=start_torrent_file_selection,
        torrent_dir=DOWNLOAD_DIR / "_torrents" / "search",
    )
    for command in ("search", "prowlarr", "tpb", "rarbg"):
        app.add_handler(CommandHandler(command, search_ui.command))
    app.add_handler(CallbackQueryHandler(search_ui.on_callback, pattern=r"^srch:"))

    # Legacy handlers
    app.add_handler(CallbackQueryHandler(handle_link_request_callback, pattern=r"^lp:"))
    app.add_handler(CallbackQueryHandler(handle_job_callback, pattern=r"^job:"))
    app.add_handler(
        CallbackQueryHandler(
            handle_stale_prompt_callback,
            pattern=r"^(ytdlp_|hentai_playlist_|pornhub_model_|manga_confirm$|manga_cancel$|spotify_confirm$|spotify_cancel$)",
        )
    )
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.FileExtension("torrent"), on_torrent_file))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    # Forwarded media is handled natively by Pyrogram (see _wrapped_post_init)
    # DO NOT register a PTB handler for forwarded messages here.

    # Text handler (catches plain text messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(error_handler)

    # Start Flask Web App mini-app server in a background thread
    if WEB_APP_ENABLE:
        try:
            default_chat_id = MINI_APP_DEFAULT_CHAT_ID or next(iter(ALLOWED_USER_IDS), None)
            # Create the loop explicitly and make it current: PTB reuses the
            # current loop in run_polling(), so run_coroutine_threadsafe() from
            # the Flask thread targets a loop that is actually running.
            bot_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(bot_loop)
            flask_app = create_web_app(
                str(DOWNLOAD_DIR),
                BOT_TOKEN,
                download_jobs=download_jobs,
                zip_jobs=mini_app_zip_jobs,
                bot_loop=bot_loop,
                bot_app=app,
                start_download=start_download_from_source,
                pause_download=pause_job,
                resume_download=resume_job,
                cancel_download=cancel_job,
                upload_selected=upload_mini_app_selection,
                zip_selected=zip_upload_mini_app_selection,
                default_chat_id=default_chat_id,
                web_app_url=WEB_APP_URL,
                allowed_user_ids=frozenset(ALLOWED_USER_IDS),
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
