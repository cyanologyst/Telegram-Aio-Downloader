"""Incoming text messages and link batches."""

from __future__ import annotations

import asyncio
import html
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import Application, ContextTypes

from app.bot import telegram_bot as tb
from app.bot.icons import ICON_FAIL, ICON_STOP, ICON_WARN
from app.bot.views import files as file_views
from app.bot.views import home as home_views
from app.infrastructure.aria2_rpc import Aria2RpcError
from app.services.link_batch import LinkBatch


async def start_batch_link(app: Application, chat_id: int, link: str, user_id: int) -> dict | None:
    """Start one link of a multi-link message right away; None if it isn't supported.

    No prompts: duplicates download anyway, video links use Settings › Video
    links (best quality if that is "ask"), and playlists download every item.
    """
    if link.lower().startswith("magnet:"):
        return await tb.start_aria2_download(app, chat_id, link, user_id)
    if tb.is_manga_url(link):
        return await tb.start_manga_download(app, chat_id, tb.extract_manga_url(link), user_id)
    if tb.is_spotify_url(link):
        return await tb.start_spotify_download(app, chat_id, tb.extract_spotify_url(link), user_id)
    if tb.is_hentai_playlist_url(link):
        playlist = await tb.resolve_hentai_playlist(link)
        if not playlist.urls:
            raise RuntimeError("No episode links found on this playlist page.")
        return await tb.start_hentai_playlist_download(app, chat_id, playlist, user_id)
    if tb.is_pornhub_model_url(link):
        playlist = await tb.resolve_pornhub_model_playlist(
            link, cookies_file=tb.cookies_file(), proxy=tb.YTDLP_PROXY or None
        )
        if not playlist.urls:
            raise RuntimeError("No public videos found on this PornHub model page.")
        return await tb.start_pornhub_model_download(app, chat_id, playlist, user_id)
    if tb.is_video_url(link):
        audio_only, max_height = tb.video_default_choice(user_id) or (False, None)
        return await tb.start_ytdlp_download(
            app,
            chat_id,
            link,
            audio_only=audio_only,
            user_id=user_id,
            run_in_background=True,
            max_height=max_height,
        )
    if tb.is_direct_http_download_url(link):
        return await tb.start_aria2_download(app, chat_id, link, user_id)
    if category := await asyncio.to_thread(tb.gallery_category, link):
        return await tb.start_gallery_download(app, chat_id, link, category, user_id)
    return None


def link_batch_summary(
    batch: LinkBatch, started: list[dict], failed: list[tuple[str, str]], limit: int
) -> str:
    """The reply to a multi-link message: what was queued and what was skipped."""
    lines = [f"📥 <b>{len(started)} of {len(batch.links)} links queued</b>"]
    if started:
        lines.append("Jobs " + ", ".join(f"#{job['id']}" for job in started))
        lines.append(
            f"Up to {limit} download{'s' if limit != 1 else ''} at once; "
            "the rest show “Queued (position n)” until a slot frees up."
        )
    if batch.duplicates:
        lines.append(f"Repeated links skipped: {batch.duplicates}")
    if failed:
        lines += ["", f"{ICON_FAIL} <b>Not started ({len(failed)})</b>"]
        lines += [
            f"• {html.escape(tb.shorten(link, 60))}: {html.escape(tb.shorten(reason, 120))}"
            for link, reason in failed[:15]
        ]
        if len(failed) > 15:
            lines.append(f"… and {len(failed) - 15} more")
    if batch.invalid:
        lines += ["", f"{ICON_WARN} <b>Lines without a link ({len(batch.invalid)})</b>"]
        lines += [f"• {html.escape(tb.shorten(line, 60))}" for line in batch.invalid[:10]]
        if len(batch.invalid) > 10:
            lines.append(f"… and {len(batch.invalid) - 10} more")
    return "\n".join(lines)


async def start_link_batch(app: Application, chat_id: int, user_id: int, batch: LinkBatch) -> str:
    """Start every link of a multi-link message in order; returns the summary text.

    Each job gets its own card; jobs over Settings › Downloads at once wait
    and show "Queued (position n)". A bad link is reported, not fatal.
    """
    started: list[dict] = []
    failed: list[tuple[str, str]] = []
    for link in batch.links:
        try:
            job = await tb.start_batch_link(app, chat_id, link, user_id)
        except Exception as exc:
            tb.logger.warning("Link batch: %s failed to start: %s", link, exc)
            failed.append((link, str(exc) or type(exc).__name__))
            continue
        if job is None:
            failed.append((link, "not a supported link"))
            continue
        started.append(job)
        try:
            await tb.attach_job_card(app, job)
        except Exception as exc:
            tb.logger.debug("Link batch: no card for #%s: %s", job.get("id"), exc)
    return tb.link_batch_summary(batch, started, failed, tb.limit_of(tb.get_user_settings(user_id)))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    lower = text.lower()

    # Check if user is waiting for password input (PRIORITY: this must be checked BEFORE zip name)
    if (
        user_id in tb.zip_select_sessions
        and tb.zip_select_sessions[user_id].get("waiting_for") == "password"
    ):
        if lower == "none":
            await tb.update_setting(user_id, "password", "")
            await update.message.reply_text("✅ Password removed")
        else:
            if not tb.validate_password(text):
                await update.message.reply_text("❌ Password too long (max 100 characters)")
                return
            settings = tb.get_user_settings(user_id)
            warn = tb.check_password_support(text, settings.get("zip_method", "zip"))
            await tb.update_setting(user_id, "password", text)
            msg = "✅ Password set"
            if warn:
                msg += f"\n⚠️ {warn}"
            await update.message.reply_text(msg)

        session = tb.zip_select_sessions.get(user_id, {})
        session.pop("waiting_for", None)
        tb.zip_select_sessions[user_id] = session
        return

    # Waiting for an archive name
    if user_id in tb.pending_zip_name_sessions:
        safe_name = tb.sanitize_archive_name(text)
        if not safe_name:
            await update.message.reply_text(
                "Please send a name made of letters or numbers (up to 100 characters), or /cancel."
            )
            return
        status_msg = await update.message.reply_text(f"📦 Creating {safe_name}...")
        tb.run_in_background(
            tb.run_named_zip(context.application, user_id, safe_name, status_msg),
            name="zip",
            on_error=tb.message_error_reporter(
                context.application, chat_id, status_msg.message_id, "Archive"
            ),
        )
        return

    # A link always wins over a pending search, rename or find prompt;
    # anything else typed while one is open is its answer.
    batch = tb.split_links(text) if "\n" in text or text.count("://") > 1 else LinkBatch()
    is_link = lower.startswith("magnet:") or tb.is_http_url(text) or batch.is_batch
    is_keyboard_button = text in tb.KEYBOARD_LABELS

    if tb.target_waiting(context):
        ref = tb.forwarded_chat_id(update.message) or tb.parse_chat_ref(text)
        if ref is not None:
            await tb.set_upload_target(update, context, ref)
            return
        if not (is_link or is_keyboard_button):
            await update.message.reply_text(
                f"{ICON_FAIL} Send @username, a t.me link or a numeric chat id "
                "(like -1001234567890), or /cancel."
            )
            return
        context.user_data.pop("target_wait", None)
    if is_link or is_keyboard_button:
        context.user_data.pop("files_wait", None)
    elif rename := tb.files_wait(context, "rename"):
        try:
            new_rel = await asyncio.to_thread(tb.rename_file, rename["rel"], text)
        except (ValueError, FileNotFoundError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "That file is gone."
            await update.message.reply_text(f"{ICON_FAIL} {reason} Send another name or /cancel.")
            return
        context.user_data.pop("files_wait", None)
        await tb.show_file(
            update.message, new_rel, rename.get("page", 0), edit=False, user_id=user_id
        )
        return
    elif tb.files_wait(context, "find"):
        context.user_data.pop("files_wait", None)
        results = await asyncio.to_thread(tb.find_files, text)
        await tb.show_screen(
            update.message,
            file_views.find_results_screen(text, results, tb.encode_path),
            edit=False,
        )
        return
    if (is_link or is_keyboard_button) and tb.search_ui is not None:
        tb.search_ui.cancel_waiting(context)
    elif tb.search_ui is not None and await tb.search_ui.handle_text(update, context):
        return

    if batch.is_batch:
        summary = await tb.start_link_batch(context.application, chat_id, user_id, batch)
        await update.message.reply_text(
            summary, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
        return

    # normalize button text by removing emojis and extra spaces
    normalized = re.sub(r"[^\w\s\u0600-\u06FF]", "", lower).strip()

    try:
        if lower.startswith("magnet:"):
            name = tb.extract_bt_name(text)
            if tb.is_duplicate_name(name):
                rid = tb.store_link_request("aria2", chat_id, source=text)
                await update.message.reply_text(
                    f"{ICON_WARN} A file named “{name}” is already in your downloads.",
                    reply_markup=tb.link_request_markup(rid, [("📥 Download anyway", "go")]),
                )
                return

            job = await tb.start_aria2_download(context.application, chat_id, text, user_id)
            await tb.attach_job_card(context.application, job)

        elif tb.is_manga_url(text):
            manga_url = tb.extract_manga_url(text)
            rid = tb.store_link_request("manga", chat_id, url=manga_url)
            await update.message.reply_text(
                tb.build_manga_prompt_text(manga_url),
                reply_markup=tb.link_request_markup(rid, [("📥 Download", "go")]),
                disable_web_page_preview=True,
            )

        elif tb.is_spotify_url(text):
            spotify_url = tb.extract_spotify_url(text)
            rid = tb.store_link_request("spotify", chat_id, url=spotify_url)
            await update.message.reply_text(
                tb.build_spotify_prompt_text(spotify_url),
                reply_markup=tb.link_request_markup(rid, [("📥 Download", "go")]),
                disable_web_page_preview=True,
            )

        elif tb.is_hentai_playlist_url(tb.extract_http_url(text)):
            playlist_url = tb.extract_http_url(text)
            try:
                playlist = await tb.resolve_hentai_playlist(playlist_url)
                if not playlist.urls:
                    raise RuntimeError("No episode links found on this playlist page.")
            except Exception as exc:
                await update.message.reply_text(
                    (
                        f"{ICON_FAIL} Playlist detection failed.\n\n"
                        f"Reason:\n{tb.shorten(str(exc), 900)}"
                    ),
                    reply_markup=tb.build_reply_menu(user_id),
                    disable_web_page_preview=True,
                )
                return

            rid = tb.store_link_request("hentai", chat_id, playlist=playlist)
            await update.message.reply_text(
                tb.build_hentai_playlist_prompt_text(playlist, user_id),
                reply_markup=tb.link_request_markup(
                    rid, [(f"📥 Download all {len(playlist.urls)}", "go")]
                ),
                disable_web_page_preview=True,
            )

        elif tb.is_pornhub_model_url(tb.extract_http_url(text)):
            model_url = tb.extract_http_url(text)
            try:
                playlist = await tb.resolve_pornhub_model_playlist(
                    model_url,
                    cookies_file=tb.cookies_file(),
                    proxy=tb.YTDLP_PROXY or None,
                )
                if not playlist.urls:
                    raise RuntimeError("No public videos found on this PornHub model page.")
            except Exception as exc:
                await update.message.reply_text(
                    (
                        f"{ICON_FAIL} PornHub model detection failed.\n\n"
                        f"Reason:\n{tb.shorten(str(exc), 900)}"
                    ),
                    reply_markup=tb.build_reply_menu(user_id),
                    disable_web_page_preview=True,
                )
                return

            rid = tb.store_link_request("pornhub", chat_id, playlist=playlist)
            await update.message.reply_text(
                tb.build_pornhub_model_prompt_text(playlist, user_id),
                reply_markup=tb.link_request_markup(
                    rid, [(f"📥 Download all {len(playlist.urls)}", "go")]
                ),
                disable_web_page_preview=True,
            )

        elif tb.is_video_url(text):
            video_url = tb.extract_http_url(text)
            platform = tb.video_platform_label(video_url)
            choice = tb.video_default_choice(user_id)
            if choice is not None:
                audio_only, max_height = choice
                job = await tb.start_ytdlp_download(
                    context.application,
                    chat_id,
                    video_url,
                    audio_only=audio_only,
                    user_id=user_id,
                    run_in_background=True,
                    max_height=max_height,
                )
                await tb.attach_job_card(context.application, job)
            else:
                rid = tb.store_link_request("video", chat_id, url=video_url)
                prompt = await update.message.reply_text(
                    f"🎬 {platform} link · checking available qualities…",
                    reply_markup=tb.link_request_markup(rid, []),
                    disable_web_page_preview=True,
                )
                tb.run_in_background(
                    tb.show_quality_picker(prompt, rid, video_url, platform), name="quality-probe"
                )

        elif tb.is_direct_http_download_url(text):
            direct_url = tb.extract_http_url(text)
            name = tb.extract_http_filename(direct_url)
            if tb.is_duplicate_name(name):
                rid = tb.store_link_request("aria2", chat_id, source=direct_url)
                await update.message.reply_text(
                    f"{ICON_WARN} A file named “{name}” is already in your downloads.",
                    reply_markup=tb.link_request_markup(rid, [("📥 Download anyway", "go")]),
                )
                return

            job = await tb.start_aria2_download(context.application, chat_id, direct_url, user_id)
            await tb.attach_job_card(context.application, job)

        elif normalized in ("menu", "home", "main menu", "downloads", "download", "tools", "tool"):
            # "downloads"/"tools" are labels from the old keyboard.
            await tb.show_screen(update.message, tb.build_home_screen(), edit=False)

        elif normalized == "status":
            await tb.show_status_dashboard(context.application, chat_id, user_id)

        elif normalized in ("settings", "setting", "manga settings", "manga"):
            await tb.show_screen(update.message, tb.build_settings_screen(user_id), edit=False)

        elif normalized == "queue":
            await update.message.reply_text(
                tb.build_queue_text(user_id), reply_markup=tb.build_reply_menu(user_id)
            )

        elif normalized in ("files", "file browser"):
            await tb.show_folder(update.message, "", 0, edit=False)

        elif normalized == "cancel":
            active = [j for j in tb.download_jobs.values() if j["status"] in tb.JOB_ACTIVE_STATES]
            if not active:
                await update.message.reply_text(
                    f"{ICON_STOP} No active downloads to cancel.",
                    reply_markup=tb.build_reply_menu(user_id),
                )
            else:
                await tb.show_status_dashboard(context.application, chat_id, user_id)

        elif lower.startswith("cancel "):
            m = re.match(r"cancel\s+#?(\d+)", lower)
            jid = int(m.group(1)) if m else None
            if jid not in tb.download_jobs:
                await update.message.reply_text(
                    (
                        "Usage: cancel <job number>, for example: cancel 3"
                        if jid is None
                        else f"Job #{jid} not found."
                    ),
                    reply_markup=tb.build_reply_menu(user_id),
                )
                return
            job = tb.download_jobs[jid]
            await update.message.reply_text(
                f"{ICON_WARN} Cancel job #{jid}?\n\n{tb.shorten(tb.clean_download_name(job['name']), 90)}\n"
                f"Status: {job['status']}",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                f"{ICON_STOP} Yes, cancel it", callback_data=f"cancel_confirm:{jid}"
                            ),
                            InlineKeyboardButton("Keep it", callback_data="noop_close"),
                        ]
                    ]
                ),
            )

        elif normalized == "clear":
            await tb.clear_jobs_cmd(update, context)

        elif normalized == "help":
            await tb.show_screen(
                update.message, home_views.help_screen(tb.SUPPORTED_SITES_URL), edit=False
            )

        elif normalized in ("zip menu", "zip", "archive"):
            context.user_data["archive_settings_back"] = "nav:archive"
            await tb.show_screen(
                update.message, await tb.build_archive_menu_screen(user_id), edit=False
            )

        elif normalized in (
            "search",
            "tpb search",
            "rarbg search",
            "rargb search",
            "prowlarr search",
        ):
            provider_key = normalized.split()[0] if normalized != "search" else None
            provider_key = "rarbg" if provider_key == "rargb" else provider_key
            await tb.search_ui.open(context, update.message, provider_key, edit=False)

        elif tb.is_http_url(text) and (
            category := await asyncio.to_thread(tb.gallery_category, tb.extract_http_url(text))
        ):
            gallery_url = tb.extract_http_url(text)
            rid = tb.store_link_request("gallery", chat_id, url=gallery_url, category=category)
            await update.message.reply_text(
                f"🖼 <b>{html.escape(tb.site_label(category))}</b> link\n\n"
                "Download it with gallery-dl into Download/Gallery?",
                reply_markup=tb.link_request_markup(rid, [("📥 Download", "go")]),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        elif tb.is_http_url(text):
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🌐 Supported sites", callback_data="sites:0")]]
            )
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
                reply_markup=tb.build_reply_menu(user_id),
            )

    except FileNotFoundError:
        await update.message.reply_text(
            f"{ICON_FAIL} {tb.ARIA2_BIN} was not found.",
            reply_markup=tb.build_reply_menu(user_id),
        )
    except (Aria2RpcError, RuntimeError) as e:
        tb.logger.error(f"aria2 error: {type(e).__name__}: {e}", exc_info=e)
        await update.message.reply_text(
            f"{ICON_FAIL} aria2 error:\n{tb.shorten(str(e), 900)}",
            reply_markup=tb.build_reply_menu(user_id),
        )
    except RetryAfter:
        raise  # flood control: a reply would be refused too; error_handler explains it elsewhere
    except Exception as e:
        # FIX #2: Sanitize error messages to avoid exposing internal details
        tb.logger.error(f"on_text error: {type(e).__name__}: {e}", exc_info=e)
        await update.message.reply_text(
            f"{ICON_FAIL} {tb.get_lang(user_id, 'error_occurred')}",
            reply_markup=tb.build_reply_menu(user_id),
        )
