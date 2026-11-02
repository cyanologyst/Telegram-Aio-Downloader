"""Slash command handlers."""

from __future__ import annotations

import asyncio
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from app.bot.icons import ICON_BACK, ICON_BROOM, ICON_OK, ICON_WARN
from app.bot.views import files as file_views, home as home_views, settings as settings_views
from app.bot import telegram_bot as tb


async def zip_files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/zip: open the Archive screen."""
    context.user_data["archive_settings_back"] = "nav:archive"
    await tb.show_screen(
        update.message, await tb.build_archive_menu_screen(update.effective_user.id), edit=False
    )


async def list_files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all files in Download folder."""
    user_id = update.effective_user.id
    
    if not tb.is_authorized_user(user_id):
        await update.message.reply_text("⛔ Unauthorized")
        return
    
    try:
        files_only = tb.collect_download_files()

        if not files_only:
            await update.message.reply_text("📭 Download folder is empty")
            return

        text_lines = [f"📁 Files ({len(files_only)} total):"]
        total_size = 0
        
        for i, f in enumerate(files_only[:50], 1):  # Show first 50
            rel_path = f.relative_to(tb.DOWNLOAD_DIR)
            size = f.stat().st_size
            total_size += size
            text_lines.append(f"{i}. {rel_path.name} ({tb.zip_human_size(size)})")
        
        if len(files_only) > 50:
            text_lines.append(f"\n... and {len(files_only) - 50} more files")
        
        text_lines.append(f"\n📊 Total size: {tb.zip_human_size(total_size)}")
        
        await update.message.reply_text("\n".join(text_lines))
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


def build_clear_jobs_prompt() -> tuple[str, InlineKeyboardMarkup | None]:
    finished = sum(1 for j in tb.download_jobs.values() if j["status"] not in tb.JOB_ACTIVE_STATES)
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
    text, markup = tb.build_clear_jobs_prompt()
    await update.message.reply_text(text, reply_markup=markup)


async def cancel_input_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel: stop whatever the bot is waiting for you to type or pick."""
    user_id = update.effective_user.id
    cancelled = []
    zip_session = tb.zip_select_sessions.get(user_id)
    if zip_session and zip_session.pop("waiting_for", None):
        cancelled.append("archive password entry")
    if tb.pending_zip_name_sessions.pop(user_id, None):
        cancelled.append("archive naming")
    if tb.search_ui is not None and tb.search_ui.is_waiting(context):
        tb.search_ui.cancel_waiting(context)
        cancelled.append("torrent search")
    if tb.torrent_select_sessions.pop(user_id, None):
        cancelled.append("torrent file selection")
    if context.user_data.pop("cookies_wait", None):
        cancelled.append("cookies upload")
    if context.user_data.pop("target_wait", None):
        cancelled.append("upload target")
    if wait := context.user_data.pop("files_wait", None):
        cancelled.append("file rename" if wait.get("kind") == "rename" else "file search")
    text = (
        f"{ICON_OK} Cancelled: {', '.join(cancelled)}."
        if cancelled
        else "Nothing to cancel. To stop a download, use Cancel on the 📊 Status card."
    )
    await update.message.reply_text(text, reply_markup=tb.build_reply_menu(user_id))


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Deep links from the search results arrive as "/start <payload>".
    if context.args and tb.search_ui is not None:
        if await tb.search_ui.handle_start(update, context, context.args[0]):
            return
    await update.message.reply_text(
        home_views.welcome_text(),
        reply_markup=tb.build_reply_menu(update.effective_user.id),
        parse_mode=ParseMode.HTML,
    )


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await tb.show_screen(update.message, tb.build_home_screen(), edit=False)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await tb.show_screen(update.message, home_views.help_screen(tb.SUPPORTED_SITES_URL), edit=False)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await tb.show_status_dashboard(
        context.application, update.effective_chat.id, update.effective_user.id
    )


async def supported_sites_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await tb.show_screen(update.message, tb.build_sites_screen(0), edit=False)


async def files_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await tb.show_folder(update.message, "", 0, edit=False)


async def browse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Open the modern file browser mini-app."""
    user_id = update.effective_user.id

    if not tb.is_authorized_user(user_id):
        await update.message.reply_text("Unauthorized")
        return

    # Use inline keyboard with Web App button
    button = tb.mini_app_inline_button("Open File Browser")
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
    await tb.show_screen(update.message, tb.build_settings_screen(update.effective_user.id), edit=False)


async def forwarded_posts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not tb.is_authorized_user(user_id):
        await update.message.reply_text("⛔ Unauthorized")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            f"{tb.format_forwarded_posts_setting(user_id)}\n\n"
            "Usage: /forwardedposts on|off",
            reply_markup=tb.build_reply_menu(user_id),
        )
        return

    value = args[0].lower()
    if value not in ("on", "off"):
        await update.message.reply_text("Usage: /forwardedposts on|off")
        return

    enabled = value == "on"
    await tb.update_setting(user_id, "auto_download_forwarded_posts", enabled)
    await update.message.reply_text(
        f"Forwarded post auto-download is now {'ON' if enabled else 'OFF'}.",
        reply_markup=tb.build_reply_menu(user_id),
    )


async def build_archive_menu_screen(user_id: int):
    files = await asyncio.to_thread(lambda: tb.filter_files_for_archiving(tb.collect_download_files()))
    total = sum(path.stat().st_size for path in files if path.exists())
    return file_views.archive_menu_screen(
        len(files), total, settings_views.archive_summary(tb.get_user_settings(user_id))
    )


async def start_search_download(update, context, source: str, force: bool = False) -> dict:
    """Start an aria2 download for a torrent-search result.

    Returns {"status": "duplicate", "name": ...} instead of starting when a
    file of that name already exists, unless ``force`` is set.
    """
    source_value, _ = tb._split_torrent_source(source)
    if tb._is_local_torrent_file(source_value):
        name = tb.clean_download_name(Path(source_value).name)
    elif source_value.lower().startswith(("http://", "https://")):
        name = tb.extract_http_filename(source_value)
    else:
        name = tb.extract_bt_name(source_value)
    if not force and tb.is_duplicate_name(name):
        return {"id": 0, "name": name, "status": "duplicate"}
    user_id = update.effective_user.id if update.effective_user else None
    job = await tb.start_aria2_download(context.application, update.effective_chat.id, source, user_id)
    await tb.attach_job_card(context.application, job)
    return job
