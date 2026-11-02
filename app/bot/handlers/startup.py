"""Application start-up, shutdown and the main() entrypoint."""

from __future__ import annotations

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
from telegram import BotCommand, MenuButtonWebApp, Update, WebAppInfo
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from app.bot import flood_guard
from app.bot import telegram_bot as tb
from app.bot.search import SearchUI
from app.bot.views import home as home_views
from app.downloaders.torrents.prowlarr.client import ProwlarrClient
from app.downloaders.torrents.rarbg.crawler import RARBGCrawler
from app.downloaders.torrents.tpb.crawler import TPBCrawler
from app.services.job_store import JobStore
from app.services.torrent_search import ProwlarrProvider, RARBGProvider, TPBProvider


async def post_init(app: Application):
    """Initialize bot - setup pyrogram, executor, and start auto-cleanup task."""

    if tb.search_ui is not None:
        tb.search_ui.bot_username = app.bot.username  # result links are t.me/<bot>?start=…

    # Initialize thread pool executor for zip operations
    # Using ThreadPoolExecutor instead of ProcessPoolExecutor to properly share
    # the progress object across threads. With GIL, this is safe for I/O-bound work.
    # Limit to 2 workers to prevent bot overload from concurrent zip operations
    tb.zip_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="zip_worker")

    # FIX #10: Verify pyrogram is logged in as user account (not bot)
    try:
        client = await tb.get_pyrogram_client()
        me = await client.get_me()
        if me.is_bot:
            raise RuntimeError(
                f"❌ FATAL: Pyrogram is logged in as a BOT (@{me.username}).\n"
                f"You must log in with a PERSONAL USER account instead.\n"
                f"Fix: Delete '{tb.PYRO_SESSION_NAME}.session' and restart the script.\n"
                f"Then log in with your personal Telegram account."
            )
        tb.logger.info(f"✅ Pyrogram logged in as user: @{me.username}")
    except tb.PyrogramUnavailable as exc:
        tb.logger.warning(
            "Running in bot-only mode because %s. Files up to 50 MB are sent through the "
            "Bot API; bigger uploads and forwarded-media capture need the Pyrogram login.",
            exc,
        )
    except Exception as e:
        tb.logger.warning(f"Could not verify pyrogram user account: {e}")

    # FIX #8: Start auto-cleanup task for old files
    async def cleanup_task():
        while True:
            try:
                await asyncio.sleep(3600)  # Run every hour
                await tb.auto_cleanup_old_files()
            except Exception as e:
                tb.logger.error(f"Auto-cleanup error: {e}")

    asyncio.create_task(cleanup_task())
    tb.logger.info("✅ Auto-cleanup task started")

    # Before anything posts: a block recorded by the last run is honoured
    # straight away, and results it held are posted when the block lifts.
    flood_guard.configure(tb.FLOOD_STATE_PATH)
    for held_chat in flood_guard.chats_with_held():
        tb.schedule_held_flush(app, held_chat)
    for blocked_chat in list(flood_guard._blocked_until):
        if flood_guard.blocked_for(blocked_chat):
            tb.logger.warning(
                "Telegram flood control in chat %s continues until %s",
                blocked_chat,
                flood_guard.lift_text(blocked_chat),
            )

    try:
        tb.job_store = JobStore(tb.JOB_DB_PATH)
        await tb.restore_jobs(app)
        tb.run_in_background(tb.job_persistence_loop(), name="job-persistence")
    except Exception:
        tb.logger.exception("Job persistence unavailable; jobs will not survive restarts")
        tb.job_store = None

    # The "/" menu Telegram shows next to the message box.
    try:
        await app.bot.set_my_commands(
            [BotCommand(name, description) for name, description in home_views.COMMANDS]
        )
    except Exception as exc:
        tb.logger.warning("Unable to register bot commands: %s", exc)

    if tb.WEB_APP_ENABLE and tb.WEB_APP_URL:
        try:
            if tb.WEB_APP_URL.lower().startswith("https://"):
                await app.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="Mini-App",
                        web_app=WebAppInfo(url=tb.WEB_APP_URL),
                    )
                )
                tb.logger.info("Telegram mini-app menu button configured")
            else:
                tb.logger.info(
                    "WEB_APP_URL is not HTTPS (%s); skipping native mini-app menu "
                    "button. Telegram only opens WebApp buttons over HTTPS. Use "
                    "scripts/start_with_tunnel.py for a free HTTPS URL, or open "
                    "the dashboard via the browser button.",
                    tb.WEB_APP_URL,
                )
        except Exception as exc:
            tb.logger.warning("Unable to configure Telegram mini-app menu button: %s", exc)


async def post_shutdown(app: Application):
    """Cleanup on shutdown - stop pyrogram client and shutdown executor."""

    # Shutdown zip executor gracefully
    if tb.zip_executor:
        tb.zip_executor.shutdown(wait=True)
        tb.zip_executor = None

    tb.stop_dashboard_server(getattr(app, "dashboard_server", None))
    if tb.job_store is not None:
        try:
            tb.save_jobs_now()
            tb.job_store.close()
        except Exception:
            tb.logger.exception("Final job save failed")
    await tb.stop_pyrogram_client()


class RetryingRequest(HTTPXRequest):
    """Retry a Bot API call once when the connection could not be opened.

    Only failures before anything was sent (connect errors, connect timeouts)
    are retried, so a message is never sent twice.

    Also honours Telegram's per-chat flood control (see ``flood_guard``): once
    a chat answers 429, sends and edits there fail locally until it lifts.
    """

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        params = request_data.parameters if request_data is not None else None
        chat_id = flood_guard.guarded_chat(url, params)
        api_method = url.rstrip("/").rsplit("/", 1)[-1]
        if not flood_guard.admit(chat_id, api_method):
            raise RetryAfter(int(flood_guard.blocked_for(chat_id)) + 1)
        probing = (
            chat_id is not None
            and flood_guard.is_send(api_method)
            and flood_guard.blocked_for(chat_id)
        )
        try:
            code, payload = await super().do_request(url, method, request_data, *args, **kwargs)
        except NetworkError as exc:
            if not isinstance(exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout)):
                raise
            tb.logger.info("Telegram connection failed (%s); retrying once", exc)
            await asyncio.sleep(1)
            code, payload = await super().do_request(url, method, request_data, *args, **kwargs)
        if code == 429 and chat_id is not None:
            retry_after = flood_guard.retry_after_from(payload)
            if retry_after:
                if not flood_guard.blocked_for(chat_id):
                    tb.logger.warning(
                        "Telegram flood control in chat %s: sends and edits paused for %.0fs",
                        chat_id,
                        retry_after,
                    )
                flood_guard.note(chat_id, retry_after)
        if (
            probing
            and (code == 429 or 200 <= code < 300)
            and flood_guard.send_result(chat_id, code != 429)
        ):
            tb.logger.warning(
                "Telegram takes new messages in chat %s again; edits stay paused until %s",
                chat_id,
                flood_guard.lift_text(chat_id),
            )
        return code, payload


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, RetryAfter):
        # Flood control: every reply in that chat is refused until it lifts. That
        # is not a bug in the handler, so no traceback; the user is told once in
        # Saved Messages, since the bot can't say it in its own chat.
        chat = getattr(update, "effective_chat", None)
        chat_id = getattr(chat, "id", None)
        if chat_id is not None:
            flood_guard.note(chat_id, tb._retry_seconds(err))
            tb.run_in_background(tb.announce_flood_block(chat_id), name=f"flood-notice-{chat_id}")
        tb.logger.warning("No reply in chat %s: Telegram flood control (%s)", chat_id, err)
        return
    if isinstance(err, (TimedOut, NetworkError)):
        tb.logger.warning(f"Telegram network error: {err}")
        return
    tb.logger.error(f"Telegram error: {err}", exc_info=err)


def acquire_instance_lock() -> None:
    """Exit if another copy of the bot is already running from this directory.

    Two copies fight over getUpdates (Telegram "Conflict"), the web ports and
    the Pyrogram session database, leaving the newer one half-started.
    """
    import fcntl

    lock_path = tb.BASE_DIR / "data" / "bot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+")  # noqa: SIM115 - kept open while the process owns the lock.
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.seek(0)
        other = lock_file.read().strip() or "?"
        raise SystemExit(
            f"Another bot instance (PID {other}) is already running; exiting."
        ) from None
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    tb._instance_lock_file = lock_file


def main():
    if not tb.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN missing")
    if not tb.API_ID or not tb.API_HASH:
        tb.logger.warning("API_ID/API_HASH missing: uploads over 50 MB will not be available")
    if not tb.ALLOWED_USER_IDS:
        raise RuntimeError(
            "ALLOWED_USER_IDS is empty. Set it in .env to your numeric Telegram user ID "
            "(message @userinfobot to find it). The bot refuses to start without it "
            "because anyone could otherwise control your server's downloads and files."
        )
    tb.acquire_instance_lock()

    # Configure HTTP request with proper timeouts
    request = tb.RetryingRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )

    async def _wrapped_post_init(app_ref: Application):
        await tb.post_init(app_ref)

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
                tb.logger.warning("Forwarded-media status message failed: %s", exc)
            return message_id

        # Start Pyrogram eagerly and register forwarded-media handler
        try:
            client = await tb.get_pyrogram_client()
            tb.setup_pyrogram_forwarded_downloads(
                client,
                str(tb.TELEGRAM_DIR),
                bot_id=tb.bot_id_from_token(tb.BOT_TOKEN),
                notify=notify_forwarded,
            )
        except Exception as exc:
            tb.logger.warning("Pyrogram forwarded-media handler not registered: %s", exc)

    app = (
        Application.builder()
        .token(tb.BOT_TOKEN)
        .request(request)
        .post_init(_wrapped_post_init)
        .post_shutdown(tb.post_shutdown)
        # Handle updates concurrently so one slow action (a big search, a
        # conversion) never freezes the rest of the bot.
        .concurrent_updates(True)
        .build()
    )

    if tb.WEB_DASHBOARD_ENABLE:
        try:
            app.dashboard_server = tb.start_dashboard_server(
                tb.WEB_DASHBOARD_HOST,
                tb.WEB_DASHBOARD_PORT,
                tb.download_jobs,
                tb.upload_jobs,
            )
            tb.logger.info(
                "✅ Web dashboard started at http://%s:%s",
                tb.WEB_DASHBOARD_HOST,
                tb.WEB_DASHBOARD_PORT,
            )
        except Exception as exc:
            tb.logger.warning("Unable to start web dashboard: %s", exc)

    # Runs before every other handler and stops updates from unknown users.
    app.add_handler(TypeHandler(Update, tb.authorization_gate), group=-1)

    # Core commands
    app.add_handler(CommandHandler("start", tb.start_cmd))
    app.add_handler(CommandHandler("cancel", tb.cancel_input_cmd))
    app.add_handler(CommandHandler("status", tb.status_cmd))
    app.add_handler(CommandHandler("sites", tb.supported_sites_cmd))
    app.add_handler(CommandHandler("files", tb.files_cmd))
    app.add_handler(CommandHandler("browse", tb.browse_cmd))
    app.add_handler(CommandHandler("settings", tb.settings_cmd))
    app.add_handler(CommandHandler("mangasettings", tb.settings_cmd))
    app.add_handler(CommandHandler("menu", tb.menu_cmd))
    app.add_handler(CommandHandler("help", tb.help_cmd))
    app.add_handler(CommandHandler("forwardedposts", tb.forwarded_posts_cmd))
    app.add_handler(CommandHandler("autoforward", tb.forwarded_posts_cmd))

    # Zipping feature handlers
    app.add_handler(CommandHandler("zip", tb.zip_files_cmd))
    app.add_handler(CommandHandler("list", tb.list_files_cmd))
    app.add_handler(CommandHandler("clear", tb.clear_jobs_cmd))

    # Unified torrent search (Prowlarr, TPB, RARBG-style mirror)
    tb.search_ui = SearchUI(
        [
            ProwlarrProvider(
                ProwlarrClient(tb.PROWLARR_URL, tb.PROWLARR_API_KEY, tb.PROWLARR_SEARCH_LIMIT)
            ),
            TPBProvider(TPBCrawler(tb.TPB_API_URL)),
            RARBGProvider(RARBGCrawler(tb.RARBG_BASE_URL)),
        ],
        start_download=tb.start_search_download,
        select_files=tb.start_torrent_file_selection,
        torrent_dir=tb.DOWNLOAD_DIR / "_torrents" / "search",
    )
    for command in ("search", "prowlarr", "tpb", "rarbg"):
        app.add_handler(CommandHandler(command, tb.search_ui.command))
    app.add_handler(CallbackQueryHandler(tb.search_ui.on_callback, pattern=r"^srch:"))

    # Legacy handlers
    app.add_handler(CallbackQueryHandler(tb.handle_link_request_callback, pattern=r"^lp:"))
    app.add_handler(CallbackQueryHandler(tb.handle_job_callback, pattern=r"^job:"))
    app.add_handler(CallbackQueryHandler(tb.handle_compress_callback, pattern=r"^cmp:"))
    app.add_handler(
        CallbackQueryHandler(
            tb.handle_stale_prompt_callback,
            pattern=r"^(ytdlp_|hentai_playlist_|pornhub_model_|manga_confirm$|manga_cancel$|spotify_confirm$|spotify_cancel$)",
        )
    )
    app.add_handler(CallbackQueryHandler(tb.on_button))
    app.add_handler(MessageHandler(filters.Document.FileExtension("torrent"), tb.on_torrent_file))
    app.add_handler(MessageHandler(filters.Document.ALL, tb.on_document))

    # Forwarded media is handled natively by Pyrogram (see _wrapped_post_init)
    # DO NOT register a PTB handler for forwarded messages here.

    # Text handler (catches plain text messages)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, tb.on_text))
    app.add_error_handler(tb.error_handler)

    # Start Flask Web App mini-app server in a background thread
    if tb.WEB_APP_ENABLE:
        try:
            default_chat_id = tb.MINI_APP_DEFAULT_CHAT_ID or next(iter(tb.ALLOWED_USER_IDS), None)
            # Create the loop explicitly and make it current: PTB reuses the
            # current loop in run_polling(), so run_coroutine_threadsafe() from
            # the Flask thread targets a loop that is actually running.
            bot_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(bot_loop)
            flask_app = tb.create_web_app(
                str(tb.DOWNLOAD_DIR),
                tb.BOT_TOKEN,
                download_jobs=tb.download_jobs,
                zip_jobs=tb.mini_app_zip_jobs,
                bot_loop=bot_loop,
                bot_app=app,
                start_download=tb.start_download_from_source,
                pause_download=tb.pause_job,
                resume_download=tb.resume_job,
                cancel_download=tb.cancel_job,
                upload_selected=tb.upload_mini_app_selection,
                zip_selected=tb.zip_upload_mini_app_selection,
                default_chat_id=default_chat_id,
                web_app_url=tb.WEB_APP_URL,
                allowed_user_ids=frozenset(tb.ALLOWED_USER_IDS),
            )

            def run_flask():
                # Run Flask in a separate thread with HTTPS
                import os as os_ssl

                cert_path = os_ssl.path.join(tb.BASE_DIR, "cert.pem")
                key_path = os_ssl.path.join(tb.BASE_DIR, "key.pem")

                # Check if certificates exist
                if os_ssl.path.exists(cert_path) and os_ssl.path.exists(key_path):
                    ssl_context = (cert_path, key_path)
                else:
                    ssl_context = None
                    tb.logger.warning("SSL certificates not found. Flask running without HTTPS")

                flask_app.run(
                    host=tb.WEB_APP_HOST,
                    port=tb.WEB_APP_PORT,
                    debug=False,
                    use_reloader=False,
                    threaded=True,
                    ssl_context=ssl_context,
                )

            flask_thread = threading.Thread(target=run_flask, daemon=True)
            flask_thread.start()

            tb.logger.info(
                "Flask Web App started at %s (Mini-App URL: %s)",
                f"{tb.WEB_APP_HOST}:{tb.WEB_APP_PORT}",
                tb.WEB_APP_URL,
            )
        except Exception as exc:
            tb.logger.warning("Unable to start Flask Web App: %s", exc)

    app.run_polling(close_loop=False)
