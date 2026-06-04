"""Typed environment configuration for the bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:

    def load_dotenv(*args: object, **kwargs: object) -> bool:
        return False


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
        load_dotenv(env_file)

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
        tpb_api_url=os.getenv("TPB_API_URL", "").strip(),
        allowed_user_ids=_parse_user_ids(os.getenv("ALLOWED_USER_IDS")),
        auto_cleanup_days=_parse_int(os.getenv("AUTO_CLEANUP_DAYS"), 7),
        environment=os.getenv("APP_ENV", "development").strip() or "development",
        project_root=Path(__file__).resolve().parents[2],
    )
