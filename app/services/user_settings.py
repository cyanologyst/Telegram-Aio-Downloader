"""
Zip Settings Manager - Handles user preferences for zipping operations
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from app.services.batch_download import DEFAULT_BATCH_DOWNLOAD_MODE, batch_download_mode_label

BASE_DIR = Path(__file__).resolve().parents[2]
SETTINGS_DIR = BASE_DIR / "zip_settings"
SETTINGS_DIR.mkdir(exist_ok=True)

# Default settings for new users
DEFAULT_SETTINGS = {
    "zip_part_size": 1 * 1024 * 1024 * 1024,  # 1GB
    "zip_method": "zip",  # "zip", "7z"
    "password": "",
    "auto_delete_files_after_zip": False,
    "auto_delete_zips_after_send": False,
    "auto_delete_files_after_upload": False,
    "auto_download_forwarded_posts": False,
    "batch_download_mode": DEFAULT_BATCH_DOWNLOAD_MODE.value,
    "manga_auto_convert_pdf": False,
    "manga_remove_images_after_conversion": False,
    "compression_level": 3,  # 1-9, reduced from 5 for better responsiveness
}

# Per-user locks to prevent concurrent settings updates
SETTINGS_LOCKS: dict[int, asyncio.Lock] = {}


def get_settings_path(user_id: int) -> Path:
    """Get the path to a user's settings file."""
    return SETTINGS_DIR / f"{user_id}.json"


def get_lock(user_id: int) -> asyncio.Lock:
    """Get or create lock for user settings."""
    if user_id not in SETTINGS_LOCKS:
        SETTINGS_LOCKS[user_id] = asyncio.Lock()
    return SETTINGS_LOCKS[user_id]


def get_user_settings(user_id: int) -> dict[str, Any]:
    """Load user settings from disk."""
    settings_path = get_settings_path(user_id)

    if settings_path.exists():
        try:
            with open(settings_path, encoding="utf-8") as f:
                user_settings = json.load(f)
                # Merge with defaults to ensure all keys exist
                merged = DEFAULT_SETTINGS.copy()
                merged.update(user_settings)
                return merged
        except (OSError, json.JSONDecodeError):
            return DEFAULT_SETTINGS.copy()

    return DEFAULT_SETTINGS.copy()


def save_user_settings(user_id: int, settings: dict[str, Any]) -> bool:
    """Save user settings to disk."""
    try:
        settings_path = get_settings_path(user_id)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, ensure_ascii=False)
        return True
    except OSError as e:
        print(f"Error saving settings for user {user_id}: {e}")
        return False


async def update_setting(user_id: int, key: str, value: Any) -> bool:
    """Update a single setting for a user."""
    lock = get_lock(user_id)
    async with lock:
        settings = get_user_settings(user_id)

        if key not in DEFAULT_SETTINGS:
            return False

        settings[key] = value
        return save_user_settings(user_id, settings)


async def get_setting(user_id: int, key: str) -> Any | None:
    """Get a single setting value."""
    settings = get_user_settings(user_id)
    return settings.get(key)


def format_settings_text(user_id: int) -> str:
    """Format user settings for display."""
    settings = get_user_settings(user_id)

    # Convert bytes to human readable
    part_size = settings.get("zip_part_size", DEFAULT_SETTINGS["zip_part_size"])
    part_size_mb = part_size // (1024 * 1024)

    lines = [
        "⚙️ Zip Settings",
        "",
        f"📦 Part Size: {part_size_mb} MB",
        f"📋 Method: {settings.get('zip_method', 'zip').upper()}",
        f"🔐 Password: {'Set' if settings.get('password') else 'None'}",
        f"🗑 Auto-delete after zip: {'✅' if settings.get('auto_delete_files_after_zip') else '❌'}",
        f"🗑 Auto-delete zips after send: {'✅' if settings.get('auto_delete_zips_after_send') else '❌'}",
        f"🗑 Auto-delete after upload: {'✅' if settings.get('auto_delete_files_after_upload') else '❌'}",
        f"📥 Auto-download forwarded posts: {'✅' if settings.get('auto_download_forwarded_posts') else '❌'}",
        f"Batch downloads: {batch_download_mode_label(settings.get('batch_download_mode'))}",
        f"🔨 Compression level: {settings.get('compression_level', 3)}/9",
    ]

    # Add performance note for 7z with high compression
    method = settings.get("zip_method", "zip").lower()
    comp_level = settings.get("compression_level", 3)
    if method == "7z" and comp_level >= 7:
        lines.append("⚠️ Note: 7z level 7+ is CPU-intensive, bot may be slower")

    return "\n".join(lines)


def validate_part_size(size_mb: int) -> bool:
    """Validate part size (100MB - 5GB)."""
    min_mb = 100
    max_mb = 5120
    return min_mb <= size_mb <= max_mb


def validate_password(password: str) -> bool:
    """Validate password (optional, max 100 chars)."""
    return len(password) <= 100


def validate_compression_level(level: int) -> bool:
    """Validate compression level (1-9)."""
    return 1 <= level <= 9


# Reset settings to defaults
def reset_user_settings(user_id: int) -> bool:
    """Reset user settings to defaults."""
    return save_user_settings(user_id, DEFAULT_SETTINGS.copy())
