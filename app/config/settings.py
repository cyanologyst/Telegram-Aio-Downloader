"""Typed environment configuration for the bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_env_file(env_file: str | os.PathLike[str]) -> bool:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return load_dotenv(env_file)


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration loaded from environment variables."""

    bot_token: str
    api_id: int
    api_hash: str
    pyro_session_name: str = "pyrogram_uploader"
    aria2_bin: str = "aria2c"
    aria2_rpc_host: str = "127.0.0.1"
    aria2_rpc_port: int = 6800
    aria2_rpc_secret: str = ""
    ffmpeg_bin: str = "ffmpeg"
    gallery_dl_bin: str = "gallery-dl"
    rclone_bin: str = "rclone"
    jdownloader_api_url: str = ""
    jdownloader_api_token: str = ""
    file_link_secret: str = ""
    file_link_base_url: str = ""
    rss_poll_interval_seconds: int = 900
    tpb_api_url: str = ""
    allowed_user_ids: frozenset[int] = frozenset()
    auto_cleanup_days: int = 7
    environment: str = "development"
    project_root: Path = Path.cwd()

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


def _parse_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _parse_user_ids(value: str | None) -> frozenset[int]:
    ids: set[int] = set()
    for chunk in (value or "").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            ids.add(int(chunk))
    return frozenset(ids)


def load_settings(env_file: str | os.PathLike[str] | None = ".env") -> Settings:
    """Load settings from ``env_file`` and process environment."""
    if env_file:
        _load_env_file(env_file)

    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        api_id=_parse_int(os.getenv("API_ID")),
        api_hash=os.getenv("API_HASH", "").strip(),
        pyro_session_name=os.getenv("PYRO_SESSION_NAME", "pyrogram_uploader").strip(),
        aria2_bin=os.getenv("ARIA2_BIN", "aria2c").strip(),
        aria2_rpc_host=os.getenv("ARIA2_RPC_HOST", "127.0.0.1").strip() or "127.0.0.1",
        aria2_rpc_port=_parse_int(os.getenv("ARIA2_RPC_PORT"), 6800),
        aria2_rpc_secret=os.getenv("ARIA2_RPC_SECRET", "").strip(),
        ffmpeg_bin=os.getenv("FFMPEG_BIN", "ffmpeg").strip(),
        gallery_dl_bin=os.getenv("GALLERY_DL_BIN", "gallery-dl").strip(),
        rclone_bin=os.getenv("RCLONE_BIN", "rclone").strip(),
        jdownloader_api_url=os.getenv("JDOWNLOADER_API_URL", "").strip().rstrip("/"),
        jdownloader_api_token=os.getenv("JDOWNLOADER_API_TOKEN", "").strip(),
        file_link_secret=os.getenv("FILE_LINK_SECRET", "").strip(),
        file_link_base_url=os.getenv("FILE_LINK_BASE_URL", "").strip().rstrip("/"),
        rss_poll_interval_seconds=_parse_int(os.getenv("RSS_POLL_INTERVAL_SECONDS"), 900),
        tpb_api_url=os.getenv("TPB_API_URL", "").strip(),
        allowed_user_ids=_parse_user_ids(os.getenv("ALLOWED_USER_IDS")),
        auto_cleanup_days=_parse_int(os.getenv("AUTO_CLEANUP_DAYS"), 7),
        environment=os.getenv("APP_ENV", "development").strip() or "development",
        project_root=Path(__file__).resolve().parents[2],
    )
