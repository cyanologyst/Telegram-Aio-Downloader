"""Shared helpers: auth, i18n, sizes, path tokens, URLs, background tasks."""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import unquote_plus

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ApplicationHandlerStop, ContextTypes

from app.bot.icons import ICON_FAIL
from app.bot import telegram_bot as tb


def store_link_request(kind: str, chat_id: int, **payload) -> str:
    now = time.time()
    for rid in [r for r, req in tb.link_requests.items() if now - req["created"] > tb.LINK_REQUEST_TTL]:
        tb.link_requests.pop(rid, None)
    rid = uuid.uuid4().hex[:10]
    tb.link_requests[rid] = {"kind": kind, "chat_id": chat_id, "created": now, **payload}
    return rid


def link_request_markup(rid: str, actions: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """Buttons for a link prompt: the given (label, action) pairs plus Cancel."""
    buttons = [InlineKeyboardButton(label, callback_data=f"lp:{rid}:{action}") for label, action in actions]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("✖ Cancel", callback_data=f"lp:{rid}:cancel")])
    return InlineKeyboardMarkup(rows)


def get_lang(user_id: int, key: str) -> str:
    """UI string by key. The bot is English-only; user_id is kept for call sites."""
    return tb.LANGUAGES[tb.DEFAULT_LANGUAGE].get(key, key)


def is_authorized_user(user_id: int) -> bool:
    # main() refuses to start with an empty allow-list, so an empty set here
    # means "nobody", never "everybody".
    return user_id in tb.ALLOWED_USER_IDS


async def authorization_gate(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop every update that does not come from an allowed user.

    Registered in handler group -1 so it runs before any command, message or
    button handler. Unauthorized users are ignored silently: replying would
    only confirm the bot exists.
    """
    user = getattr(update, "effective_user", None)
    if user is not None and tb.is_authorized_user(user.id):
        return
    if user is not None:
        tb.logger.warning("Ignored update from unauthorized user %s", user.id)
    raise ApplicationHandlerStop


async def auto_cleanup_old_files():
    now = time.time()
    max_age = tb.AUTO_CLEANUP_DAYS * 86400

    for root, _, files in os.walk(tb.DOWNLOAD_DIR):
        for file_name in files:
            path = Path(root) / file_name

            try:
                age = now - path.stat().st_mtime

                if age > max_age:
                    if "_part_" in file_name or ".thumb_" in file_name:
                        path.unlink(missing_ok=True)

            except Exception:
                continue


def clean_emoji_prefix(text: str) -> str:
    """Remove leading emoji/icon prefixes to avoid duplicated icons in UI."""
    if not text:
        return text
    return tb.EMOJI_PREFIX_RE.sub("", text).strip()


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
        return sorted([f for f in tb.DOWNLOAD_DIR.rglob("*") if f.is_file()])
    except Exception:
        return []


def encode_path(rel_path: str) -> str:
    """FIX #4: Encode path with timestamp-based token expiration to prevent memory leaks."""

    if not rel_path:
        return ""

    rel_path = str(rel_path)

    # Check if we already have a valid token for this path
    if rel_path in tb.reverse_path_tokens:
        token = tb.reverse_path_tokens[rel_path]
        if token in tb.path_tokens:
            stored_path, timestamp = tb.path_tokens[token]
            if time.time() - timestamp < tb.PATH_TOKEN_TIMEOUT:
                # Token is still valid, reuse it
                return token

    # Clean up expired tokens to prevent memory leaks
    current_time = time.time()
    expired_tokens = [token for token, (path, ts) in tb.path_tokens.items() 
                      if current_time - ts >= tb.PATH_TOKEN_TIMEOUT]
    for token in expired_tokens:
        stored_path, _ = tb.path_tokens.pop(token, (None, None))
        if stored_path in tb.reverse_path_tokens:
            del tb.reverse_path_tokens[stored_path]

    # Create new token
    tb.path_token_counter += 1
    path_token_counter_val = tb.path_token_counter
    token = f"p{path_token_counter_val}"

    tb.path_tokens[token] = (rel_path, current_time)
    tb.reverse_path_tokens[rel_path] = token

    return token


def decode_path(token: str) -> str:
    if not token:
        return ""

    if token not in tb.path_tokens:
        raise ValueError("Path token expired. Please open Files again.")

    rel_path, timestamp = tb.path_tokens[token]
    
    # Check if token has expired
    if time.time() - timestamp >= tb.PATH_TOKEN_TIMEOUT:
        # Clean up expired token
        del tb.path_tokens[token]
        if rel_path in tb.reverse_path_tokens:
            del tb.reverse_path_tokens[rel_path]
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
        return tb.clean_download_name(unquote_plus(m.group(1)))
    except Exception:
        return "Unknown torrent"


def clean_download_name(name: str) -> str:
    name = unquote_plus(str(name or "")).strip()
    name = re.sub(r"^\[metadata\]\s*", "", name, flags=re.I)
    name = re.sub(r"\s+", " ", name.replace("+", " ")).strip()
    return name or "Unknown torrent"


def extract_http_url(text: str) -> str:
    match = re.search(r"https?://\S+", text.strip(), re.IGNORECASE)
    if not match:
        return text.strip()
    return match.group(0).rstrip(").,]}")


def is_http_url(text: str) -> bool:
    return tb.extract_http_url(text).lower().startswith(("http://", "https://"))


def extract_http_filename(url: str) -> str:
    url = tb.extract_http_url(url)
    clean_url = url.split("?", 1)[0].split("#", 1)[0]
    name = Path(unquote_plus(clean_url)).name
    return tb.clean_download_name(name) if name else "HTTP download"


def is_direct_http_download_url(text: str) -> bool:
    if not tb.is_http_url(text):
        return False
    path = tb.extract_http_url(text).split("?", 1)[0].split("#", 1)[0]
    return Path(path).suffix.lower() in tb.DIRECT_HTTP_EXTENSIONS


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


def shorten(text: str, max_len: int = 38) -> str:
    text = text.strip()
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def get_all_files_in_folder(rel_path: str):
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    if not full.is_dir():
        raise NotADirectoryError(str(full))

    out = []
    for root, _, files in os.walk(full):
        for f in sorted(files):
            p = Path(root) / f
            try:
                if p.is_file():
                    rel = str(p.relative_to(tb.DOWNLOAD_DIR))
                    if not tb.is_internal(rel):
                        out.append(rel)
            except Exception:
                pass
    return sorted(out)


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
            tb.logger.exception("Background task %s failed", name)
            if on_error is not None:
                try:
                    await on_error(exc)
                except Exception:
                    tb.logger.exception("Error reporter for %s failed", name)

    task = asyncio.create_task(runner(), name=name)
    tb.background_tasks.add(task)
    task.add_done_callback(tb.background_tasks.discard)
    return task


def message_error_reporter(app: Application, chat_id: int, message_id: int, what: str):
    """on_error callback that turns a progress message into a failure notice."""

    async def report(exc: Exception) -> None:
        # Without a progress message (flood control kept it from being sent)
        # it is posted new; while the chat is blocked it is held until it lifts.
        await tb.show_or_hold(
            app, chat_id, message_id,
            f"{ICON_FAIL} {what} failed.\n\nReason:\n{tb.shorten(str(exc), 600)}",
            "files",
        )

    return report
