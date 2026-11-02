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
import weakref
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
from yt_dlp.networking.impersonate import ImpersonateTarget

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
from pyrogram import enums as pyro_enums
from pyrogram.errors import FloodWait, RPCError
import httpx
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaVideo,
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

from app.bot import flood_guard
from app.bot.callbacks import answer_once, auto_answer
from app.bot.search import SearchUI
from app.bot.views import files as file_views
from app.bot.views import compress as compress_views
from app.bot.views import home as home_views
from app.bot.views import jobs as job_views
from app.bot.views import settings as settings_views
from app.bot.views.common import Screen
from app.downloaders.torrents.prowlarr.client import ProwlarrClient
from app.downloaders.torrents.rarbg.crawler import RARBGCrawler
from app.downloaders.torrents.tpb.crawler import TPBCrawler

# Import post downloader module for handling forwarded posts
from app.handlers.forwarded_media import bot_id_from_token, setup_pyrogram_forwarded_downloads
from app.infrastructure.aria2_rpc import (
    DEFAULT_BT_TRACKERS,
    Aria2ConnectionError,
    Aria2DaemonConfig,
    Aria2RpcClient,
    Aria2RpcError,
)
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
from app.services import gif_previews
from app.services import upload_filter
from app.services import upload_guard
from app.services import supported_sites
from app.services import video_compress
from app.services import video_split
from app.services.link_batch import LinkBatch, split_links
from app.services.download_slots import DownloadSlots, limit_of
from app.services import disk_guard
from app.services.edit_throttle import AdaptiveThrottle
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
    parse_chat_ref,
    update_setting,
    upload_target,
    upload_target_label,
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
    requires_ytdlp_impersonation,
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
# The most one upload may weigh (Premium); each upload checks the account's own cap.
MAX_SEND_SIZE = video_split.PREMIUM_PART_CAP

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
FLOOD_STATE_PATH = JOB_DB_PATH.with_name("flood_control.json")
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
        "target_val": "Saved Messages, or the channel/group set in Settings › Upload to",
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
        "upload_account": "- Uploads go to Saved Messages unless a channel/group is set in Settings.",
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
        "target_account": "Target: Saved Messages or the channel/group set in Settings",
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


# =========================================================
# Emoji-safe UI constants
# =========================================================

from app.bot.icons import (  # noqa: F401  (re-exported)
    ICON_HOME,
    ICON_FOLDER,
    ICON_FILE,
    ICON_VIDEO,
    ICON_AUDIO,
    ICON_IMAGE,
    ICON_ARCHIVE,
    ICON_MAGNET,
    ICON_STATUS,
    ICON_QUEUE,
    ICON_UPLOAD,
    ICON_DOWNLOAD,
    ICON_DELETE,
    ICON_INFO,
    ICON_PIN,
    ICON_BOX,
    ICON_SPEED,
    ICON_CLOCK,
    ICON_WARN,
    ICON_OK,
    ICON_FAIL,
    ICON_UP,
    ICON_BACK,
    ICON_NEXT,
    ICON_REFRESH,
    ICON_HELP,
    ICON_BROOM,
    ICON_STOP,
    ICON_SETTINGS,
    ICON_LANGUAGE,
)


EMOJI_PREFIX_RE = re.compile(r"^[\W_]*[\U0001F300-\U0001FAFF\u2600-\u27BF\u2139\uFE0F]+\s*")


# =========================================================
# Helpers
# =========================================================


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


# =========================================================
# Job cards: one live message per download
# =========================================================

# Telegram allows roughly one edit per second per chat. Cards share that
# budget, so the more jobs are running, the less often each card refreshes.
CARD_MIN_INTERVAL = 4.0
# A live message is edited when progress moves this many percent, or at
# least this often while it changes; the gap scales with concurrent tasks.
STATUS_MILESTONE_PERCENT = parse_env_int("STATUS_MILESTONE_PERCENT", 5)
STATUS_HEARTBEAT_SECONDS = parse_env_int("STATUS_HEARTBEAT_SECONDS", 15)
edit_throttle = AdaptiveThrottle(
    min_interval=CARD_MIN_INTERVAL,
    per_task=1.5,
    max_interval=STATUS_HEARTBEAT_SECONDS,
    milestone=STATUS_MILESTONE_PERCENT,
)


GIFS_PER_JOB = 10
GIF_PROVIDERS = {"yt-dlp", "gallery-dl"}
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "").strip() or str(
    Path(FFMPEG_BIN).with_name("ffprobe") if os.sep in FFMPEG_BIN else "ffprobe"
)


# Folders the bot keeps under Download/ even when empty.
KEPT_FOLDERS = {
    p.name for p in (TELEGRAM_DIR, SPOTIFY_DIR, MANGA_DIR, ADULT_VIDEO_DIR, HENTAI_VIDEO_DIR, GALLERY_DIR)
}


_preview_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


_send_chains: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" = weakref.WeakKeyDictionary()


# =========================================================
# Compress (H.265): re-encode a video smaller, with a live card
# =========================================================

compress_jobs: dict[str, dict] = {}
_compress_counter = 0
_compress_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


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


COOKIE_WAIT_SECONDS = 600


PYRO_GROUP_TYPES = {
    pyro_enums.ChatType.CHANNEL,
    pyro_enums.ChatType.SUPERGROUP,
    pyro_enums.ChatType.GROUP,
    pyro_enums.ChatType.FORUM,
}


# =========================================================
# File browser
# =========================================================

# Kept out of the file browser, archives and bulk deletes: aria2's RPC secret
# and other dotfiles, and the .torrent files aria2 is still using.
INTERNAL_ROOT_NAMES = {"_torrents"}


# Library folders the bot manages itself; "Organize" at the Download root skips them.
ORGANIZE_PROTECTED_NAMES = {"Telegram", "Spotify", "Manga", "Adult", "Hentai", "Gallery", "_torrents"}


files_sort: dict[int, str] = {}  # chat id -> "new" | "name" | "size"
FIND_LIMIT = 30


FILES_WAIT_SECONDS = 600


# =========================================================
# Upload helpers
# =========================================================


UPLOAD_GUARD_STATUS_KEYS = ["gid", "status", "files"]


# =========================================================
# Video conversion helpers (new)
# =========================================================


# =========================================================
# yt-dlp video downloader
# =========================================================


# yt-dlp needs a JavaScript runtime for YouTube; it only looks for Deno by default.
YTDLP_JS_RUNTIMES = {
    name: {} for name in ("deno", "node", "bun") if shutil.which(name)
}


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


cobalt_client = CobaltClient(COBALT_API_URL, COBALT_API_KEY)
COBALT_UNSUPPORTED = {"error.api.link.unsupported", "error.api.service.unsupported"}


SITE_BLOCK_PHRASES = ("blocked by network security", "403 forbidden", "http error 403", "'403'")


download_slots = DownloadSlots()

# Disk guard: every download needs its size + this buffer free. When short,
# the oldest finished files under Download/ are removed first (unless
# auto-purge is off); still short, the job is refused or held paused.
DISK_GUARD_ENABLE = os.getenv("DISK_GUARD_ENABLE", "true").strip().lower() in {"1", "true", "yes", "on"}
DISK_GUARD_AUTO_PURGE = os.getenv("DISK_GUARD_AUTO_PURGE", "true").strip().lower() in {"1", "true", "yes", "on"}
DISK_GUARD_BUFFER_GB = max(0, parse_env_int("DISK_GUARD_BUFFER_GB", 3))
# Paths under Download/ the purge never touches (comma-separated, relative).
DISK_GUARD_PROTECTED = [
    p.strip().strip("/")
    for p in os.getenv("DISK_GUARD_PROTECTED", "_torrents").split(",")
    if p.strip().strip("/")
]
_disk_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


# =========================================================
# Background tasks
# =========================================================

# Strong references so running tasks are not garbage-collected mid-flight.
background_tasks: set[asyncio.Task] = set()


# =========================================================
# Pyrogram
# =========================================================

MAX_FLOOD_WAIT = 300  # seconds; a longer FloodWait fails the send instead of blocking uploads


_pyro_start_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()


PYROGRAM_BOT_SESSION_HINT = (
    "Pyrogram is logged in as a bot.\n"
    f'Delete "{PYRO_SESSION_NAME}.session" and restart the script.\n'
    "Then log in with your personal Telegram account."
)


MAX_FINISHED_UPLOADS = 100


# Buttons a held message gets when it is finally posted (markups aren't JSON).
_HELD_MARKUPS = {
    "upload_done": lambda: upload_done_markup(),
    "files": lambda: InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{ICON_FOLDER} Files", callback_data="fb:list:0:")]]
    ),
}
HELD_SPACING = 2.0  # seconds between held messages, so posting them doesn't re-trigger flood control
HELD_MAX_ATTEMPTS = 5
HELD_RECHECK = 30.0  # seconds between checks whether a blocked chat takes new messages again
_held_flushers: dict[int, asyncio.Task] = {}


# =========================================================
# Aria2 RPC daemon manager
# =========================================================

ARIA2_DONE_STATES = {"complete", "error", "removed"}
JOB_ACTIVE_STATES = job_views.ACTIVE


TORRENT_UNREACHABLE_AFTER = 5 * 60  # seconds with no peers and no data


MAGNET_RESOLVE_AFTER = 60  # seconds a magnet may sit at 0 peers before we fetch its .torrent
ITORRENTS_URL = "https://itorrents.org/torrent/{}.torrent"


# =========================================================
# Zipping Feature - Handlers
# =========================================================


# =========================================================
# Commands
# =========================================================


# =========================================================
# Zip Menu Functions
# =========================================================


# =========================================================
# Text handler
# =========================================================


TORRENT_FILES_PER_PAGE = 8


# =========================================================
# Callback handler
# =========================================================


# =========================================================
# Main
# =========================================================


_instance_lock_file = None


# =========================================================
# Handlers (re-exported so `telegram_bot.<name>` keeps working)
# =========================================================

from app.bot.handlers.common import (  # noqa: E402,F401
    store_link_request,
    link_request_markup,
    get_lang,
    is_authorized_user,
    authorization_gate,
    auto_cleanup_old_files,
    clean_emoji_prefix,
    human_size,
    safe_join,
    collect_download_files,
    encode_path,
    decode_path,
    rel_parent,
    rel_name,
    extract_bt_name,
    clean_download_name,
    extract_http_url,
    is_http_url,
    extract_http_filename,
    is_direct_http_download_url,
    now_ts,
    format_eta,
    shorten,
    get_all_files_in_folder,
    run_in_background,
    message_error_reporter,
)
from app.bot.handlers.archives import (  # noqa: E402,F401
    archive_kwargs_from_settings,
    file_rel_path,
    build_files_to_zip,
    format_archive_progress_text,
    default_archive_name,
    sanitize_archive_name,
    ask_zip_name,
    run_named_zip,
    maybe_delete_file_after_upload,
    delete_uploaded_file,
    create_zip_upload_callback,
    run_archive_job,
    send_archive_document,
    send_archives_to_chat,
)
from app.bot.handlers.status import (  # noqa: E402,F401
    _DashboardMessageGone,
    _edit_dashboard_message,
    _hash_content,
    build_status_screen,
    update_status_message,
    show_status_dashboard,
    maybe_auto_update_status_message,
)
from app.bot.handlers.job_cards import (  # noqa: E402,F401
    _abs_download_path,
    job_outputs,
    job_open_data,
    render_job_card,
    _send_card,
    attach_job_card,
    refresh_job_card,
    finish_job_card,
)
from app.bot.handlers.previews import (  # noqa: E402,F401
    gif_candidates,
    prepare_gifs,
    send_job_gifs,
    job_preview_video,
    make_gif_previews,
    preview_video_kwargs,
    send_preview_video,
    _send_preview_album,
    merge_gif_previews,
    _send_merged_preview,
    _preview_sender,
    send_gif_previews,
    _preview_lock,
    _send_chain,
    wants_upload_previews,
    send_upload_previews,
)
from app.bot.handlers.jobs import (  # noqa: E402,F401
    job_files,
    _only_leftovers,
    prune_empty_folders,
    filter_auto_upload_files,
    flood_blocked_alert,
    answer_upload_tap,
    start_job_upload,
    delete_job_outputs,
    restart_job,
    handle_job_callback,
)
from app.bot.handlers.compress import (  # noqa: E402,F401
    _compress_lock,
    _run_ffmpeg_progress,
    compress_video,
    new_compress_job,
    active_compress_for,
    refresh_compress_card,
    run_compress_job,
    start_compress_job,
    cancel_compress_job,
    replace_with_compressed,
    handle_compress_callback,
)
from app.bot.handlers.screens import (  # noqa: E402,F401
    safe_edit_message,
    mini_app_inline_button,
    build_reply_menu,
    free_disk_bytes,
    active_job_count,
    build_home_screen,
    build_settings_screen,
    build_sites_screen,
    current_cookies,
    build_cookies_screen,
    cookies_waiting,
    build_target_screen,
    target_waiting,
    forwarded_chat_id,
    _check_target_with_account,
    _check_target_with_bot,
    resolve_upload_target,
    set_upload_target,
    looks_like_cookie_file,
    on_document,
    build_archive_settings_screen,
    show_screen,
    format_forwarded_posts_setting,
    build_queue_text,
)
from app.bot.handlers.files import (  # noqa: E402,F401
    is_internal,
    list_dir,
    file_info,
    folder_info,
    active_job_names,
    build_organize_plan,
    build_organize_preview,
    _entry_for,
    recent_entries,
    browser_entries,
    show_folder,
    show_file,
    find_files,
    rename_file,
    send_file_here,
    files_wait,
    files_in_folder,
    show_selection,
    handle_file_selection,
    is_manga_gallery_folder,
    delete_path,
    delete_all_in_directory,
    delete_paths_batch,
    is_duplicate_name,
)
from app.bot.handlers.media import (  # noqa: E402,F401
    is_video_file,
    is_audio_file,
    is_image_file,
    get_send_method,
    build_upload_caption,
    build_progress_bar,
    split_file_into_chunks,
    cleanup_file_chunks,
    prepare_upload_parts,
    upload_part_cap,
    _positive_int,
    video_send_kwargs,
    get_video_metadata,
    unfinished_aria2_downloads,
    _video_metadata_readable,
    upload_refusal,
    cleanup_orphan_thumbnails,
    generate_thumbnail,
    parse_ffmpeg_duration,
    parse_ffmpeg_progress_seconds,
    convert_video_quality,
    send_thumbnail,
)
from app.bot.handlers.download_support import (  # noqa: E402,F401
    cookies_file,
    ytdlp_common_options,
    probe_video,
    explain_ytdlp_error,
    DownloadFailed,
    download_with_cobalt,
    explain_gallery_error,
    explain_spotdl_error,
    quality_actions,
    show_quality_picker,
    video_default_choice,
    video_format_selector,
    is_video_url,
    build_hentai_playlist_prompt_text,
    build_pornhub_model_prompt_text,
    extract_spotify_url,
    build_spotify_prompt_text,
    build_manga_prompt_text,
    convert_manga_folder_to_pdf_job,
    wait_for_download_slot,
    run_with_download_slot,
)
from app.bot.handlers.disk import (  # noqa: E402,F401
    disk_protected_paths,
    check_disk_space,
    ensure_job_disk_space,
    ytdlp_disk_check,
    hold_aria2_job_for_disk,
    aria2_disk_gate,
)
from app.bot.handlers.downloads import (  # noqa: E402,F401
    start_manga_download,
    start_gallery_download,
    _refresh_while_active,
    start_spotify_download,
    start_ytdlp_download,
    upload_and_delete_batch_artifact,
    run_video_batch,
)
from app.bot.handlers.playlists import (  # noqa: E402,F401
    start_hentai_playlist_download,
    start_pornhub_model_download,
    handle_link_request_callback,
    _run_link_request,
    handle_stale_prompt_callback,
    _start_download_from_source,
    start_download_from_source,
)
from app.bot.handlers.pyro import (  # noqa: E402,F401
    send_with_flood_wait_handling,
    PyrogramUnavailable,
    pyrogram_unavailable_reason,
    get_pyrogram_client,
    stop_pyrogram_client,
    pyrogram_send_file,
    _raise_if_bot_session,
)
from app.bot.handlers.uploads import (  # noqa: E402,F401
    upload_progress_markup,
    upload_done_markup,
    new_upload_job,
    cancel_upload,
    update_upload_progress,
    _send_via_bot_api,
    _upload_one_file,
    upload_files_via_pyrogram,
    _show_upload_result,
    show_or_hold,
    _post_message_now,
    _retry_seconds,
    hold_message,
    schedule_held_flush,
    _flush_held_messages,
    announce_flood_block,
    send_single_file_via_pyrogram,
    send_folder_files_via_pyrogram,
    upload_mini_app_selection,
    zip_upload_mini_app_selection,
)
from app.bot.handlers.torrents import (  # noqa: E402,F401
    _parse_int_field,
    extract_info_hash,
    _same_info_hash,
    find_aria2_status_by_info_hash,
    _format_eta,
    _extract_rpc_name,
    _apply_aria2_status,
    _split_torrent_source,
    _is_local_torrent_file,
    switch_to_followed_gid,
    fetch_torrent_file,
    maybe_resolve_magnet_via_torrent_file,
    monitor_aria2_job,
    _monitor_aria2_job,
    aria2_add_source,
    start_aria2_download,
    cancel_job,
    pause_job,
    resume_job,
    clear_finished_jobs,
    on_torrent_file,
    start_torrent_file_selection,
    parse_aria2_show_files,
    get_torrent_file_list,
    build_torrent_select_text,
    build_torrent_select_keyboard,
)
from app.bot.handlers.commands import (  # noqa: E402,F401
    zip_files_cmd,
    list_files_cmd,
    build_clear_jobs_prompt,
    clear_jobs_cmd,
    cancel_input_cmd,
    start_cmd,
    menu_cmd,
    help_cmd,
    status_cmd,
    supported_sites_cmd,
    files_cmd,
    browse_cmd,
    settings_cmd,
    forwarded_posts_cmd,
    build_archive_menu_screen,
    start_search_download,
)
from app.bot.handlers.messages import (  # noqa: E402,F401
    start_batch_link,
    link_batch_summary,
    start_link_batch,
    on_text,
)
from app.bot.handlers.buttons import (  # noqa: E402,F401
    on_button,
)
from app.bot.handlers.persistence import (  # noqa: E402,F401
    save_jobs_now,
    job_persistence_loop,
    reattach_aria2_job,
    restore_jobs,
)
from app.bot.handlers.startup import (  # noqa: E402,F401
    post_init,
    post_shutdown,
    RetryingRequest,
    error_handler,
    acquire_instance_lock,
    main,
)


if __name__ == "__main__":
    main()
