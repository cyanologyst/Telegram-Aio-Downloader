"""Menus and settings screens, cookies and upload target set-up."""

from __future__ import annotations

import asyncio
import html
import shutil
import time
from pathlib import Path

from pyrogram import enums as pyro_enums
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import ContextTypes

from app.bot.icons import ICON_QUEUE
from app.bot.views import home as home_views, settings as settings_views
from app.bot.views.common import Screen
from app.services import cookies as cookie_store, supported_sites
from app.bot import telegram_bot as tb


async def safe_edit_message(message, text, reply_markup=None, parse_mode=None):
    try:
        await message.edit_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=True,
        )
    except RetryAfter:
        # Flood control: a reply would be refused too, and prolong the block.
        return
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


def mini_app_inline_button(label: str = "📱 Mini App"):
    """HTTPS-aware inline Mini App button; falls back to a plain URL button."""
    return tb.MINI_APP.inline_button(label)


def build_reply_menu(user_id: int = None):
    return home_views.reply_keyboard(tb.MINI_APP)


def free_disk_bytes() -> int | None:
    try:
        return shutil.disk_usage(tb.DOWNLOAD_DIR).free
    except OSError:
        return None


def active_job_count() -> int:
    return sum(
        1
        for job in tb.download_jobs.values()
        if job.get("status") in tb.JOB_ACTIVE_STATES and job.get("status_visible", True)
    )


def build_home_screen() -> Screen:
    return home_views.home_screen(tb.active_job_count(), tb.free_disk_bytes(), tb.MINI_APP)


def build_settings_screen(user_id: int) -> Screen:
    return settings_views.settings_screen(tb.get_user_settings(user_id), tb.current_cookies())


def build_sites_screen(page: int = 0) -> Screen:
    lines = supported_sites.site_lines()
    shown, page, pages = supported_sites.page_of(lines, page)
    total = sum(1 for line in lines if not line.startswith("§ "))
    return home_views.sites_screen(shown, page, pages, total, tb.SUPPORTED_SITES_URL)


def current_cookies():
    path = tb.cookies_file()
    return cookie_store.read_summary(Path(path)) if path else None


def build_cookies_screen(context, *, confirm_delete: bool = False) -> Screen:
    path = tb.cookies_file()
    return settings_views.cookies_screen(
        tb.current_cookies(),
        waiting=tb.cookies_waiting(context),
        from_env=bool(path) and Path(path) != tb.UPLOADED_COOKIES_PATH,
        confirm_delete=confirm_delete,
    )


def cookies_waiting(context) -> bool:
    since = context.user_data.get("cookies_wait")
    return bool(since) and time.time() - since < tb.COOKIE_WAIT_SECONDS


def build_target_screen(user_id: int, *, waiting: bool = False, error: str | None = None) -> Screen:
    return settings_views.upload_target_screen(tb.get_user_settings(user_id), waiting=waiting, error=error)


def target_waiting(context) -> bool:
    since = context.user_data.get("target_wait")
    return bool(since) and time.time() - since < tb.FILES_WAIT_SECONDS


def forwarded_chat_id(message) -> int | None:
    """The channel or group a forwarded message came from, if Telegram tells us."""
    origin = getattr(message, "forward_origin", None)
    chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    return getattr(chat, "id", None)


async def _check_target_with_account(client, ref: int | str) -> tuple[int, str]:
    try:
        chat = await client.get_chat(ref)
    except Exception as exc:
        raise ValueError(
            f"Telegram can't find {ref} ({exc}). Use its @username, or open the chat once "
            "with your account so the numeric id is known."
        ) from exc
    if chat.type not in tb.PYRO_GROUP_TYPES:
        raise ValueError("That is not a channel or group.")
    try:
        member = await client.get_chat_member(chat.id, "me")
    except Exception as exc:
        raise ValueError("Your account is not a member of it; join it first.") from exc
    status = member.status
    is_channel = chat.type == pyro_enums.ChatType.CHANNEL
    if status == pyro_enums.ChatMemberStatus.ADMINISTRATOR:
        if is_channel and not getattr(member.privileges, "can_post_messages", False):
            raise ValueError("Your account is an admin there but can't post messages.")
    elif status == pyro_enums.ChatMemberStatus.MEMBER and is_channel:
        raise ValueError("Only admins can post in a channel; make your account an admin.")
    elif status == pyro_enums.ChatMemberStatus.RESTRICTED:
        raise ValueError("Your account is restricted there and may not be able to send files.")
    elif status not in (pyro_enums.ChatMemberStatus.OWNER, pyro_enums.ChatMemberStatus.MEMBER,
                        pyro_enums.ChatMemberStatus.ADMINISTRATOR):
        raise ValueError("Your account is not a member of it; join it first.")
    return chat.id, chat.title or chat.username or str(chat.id)


async def _check_target_with_bot(bot, ref: int | str) -> tuple[int, str]:
    try:
        chat = await bot.get_chat(ref)
        member = await bot.get_chat_member(chat.id, bot.id)
    except Exception as exc:
        raise ValueError(
            f"The bot can't open {ref} ({exc}). Add the bot to it as an admin first."
        ) from exc
    if chat.type not in ("channel", "group", "supergroup"):
        raise ValueError("That is not a channel or group.")
    status = member.status
    if chat.type == "channel":
        if status != "administrator" or not getattr(member, "can_post_messages", False):
            if status != "creator":
                raise ValueError("The bot must be an admin that can post in the channel.")
    elif status == "restricted" and not getattr(member, "can_send_documents", False):
        raise ValueError("The bot is restricted there and can't send files.")
    elif status not in ("administrator", "creator", "member", "restricted"):
        raise ValueError("The bot is not a member of that group; add it first.")
    return chat.id, chat.title or chat.username or str(chat.id)


async def resolve_upload_target(bot, ref: int | str) -> tuple[int, str]:
    """Check uploads can be posted in ``ref``: (chat id, title), or ValueError saying why.

    Uploads go through the user account (Pyrogram) when it is logged in, so
    that account must be able to post; in bot-only mode the bot must.
    """
    try:
        client = await tb.get_pyrogram_client()
    except tb.PyrogramUnavailable:
        return await tb._check_target_with_bot(bot, ref)
    return await tb._check_target_with_account(client, ref)


async def set_upload_target(update: Update, context, ref: int | str) -> None:
    """Validate and save the upload target typed or forwarded by the user."""
    user_id = update.effective_user.id
    status = await update.message.reply_text(f"🔎 Checking {ref}…")
    try:
        chat_id, title = await tb.resolve_upload_target(context.bot, ref)
    except ValueError as exc:
        await tb.show_screen(status, tb.build_target_screen(user_id, waiting=True, error=str(exc)))
        return
    context.user_data.pop("target_wait", None)
    await tb.update_setting(user_id, "target_chat_id", chat_id)
    await tb.update_setting(user_id, "target_chat_title", title)
    await tb.show_screen(status, tb.build_target_screen(user_id))


def looks_like_cookie_file(name: str | None) -> bool:
    name = (name or "").lower()
    return "cookie" in name and name.endswith(".txt")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Non-torrent files: only a cookies.txt is expected (Settings → Cookies)."""
    message = update.message
    doc = message.document
    if not (tb.cookies_waiting(context) or tb.looks_like_cookie_file(doc.file_name)):
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
        tb.logger.debug("Could not delete the cookies message: %s", exc)
        removed = "Delete your message with the file yourself; I couldn't."
    try:
        text, summary = cookie_store.parse_cookies(data)
    except cookie_store.CookieFileError as exc:
        await message.chat.send_message(f"❌ {exc}\n\n{removed}")
        return
    await asyncio.to_thread(cookie_store.save_cookies, tb.UPLOADED_COOKIES_PATH, text)
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
    return settings_views.archive_settings_screen(tb.get_user_settings(user_id), back)


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
            tb.logger.debug("Screen edit failed (%s); sending a new message", exc)
    return await message.reply_text(
        text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


def format_forwarded_posts_setting(user_id: int) -> str:
    settings = tb.get_user_settings(user_id)
    enabled = settings.get("auto_download_forwarded_posts", False)
    return f"Forwarded media auto-download: {'ON' if enabled else 'OFF'}"


def build_queue_text(user_id: int = None):
    u = user_id or 0
    if not tb.download_jobs:
        return f"{ICON_QUEUE} {tb.clean_emoji_prefix(tb.get_lang(u, 'queue'))}\n\n{tb.get_lang(u, 'no_jobs')}"

    lines = [f"{ICON_QUEUE} {tb.clean_emoji_prefix(tb.get_lang(u, 'queue'))}", ""]
    for j in sorted(tb.download_jobs.values(), key=lambda x: x["id"], reverse=True):
        lines.append(
            f"#{j['id']} [{j['status']}] {j['name']} ({j.get('progress', 0.0):.1f}%)"
        )
    return "\n".join(lines)
