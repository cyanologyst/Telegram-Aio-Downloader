"""The inline-button callback router."""

from __future__ import annotations

import asyncio
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, NetworkError, RetryAfter
from telegram.ext import ContextTypes

from app.bot import flood_guard
from app.bot import telegram_bot as tb
from app.bot.icons import (
    ICON_BROOM,
    ICON_FAIL,
    ICON_FOLDER,
    ICON_IMAGE,
    ICON_OK,
    ICON_UPLOAD,
    ICON_WARN,
)
from app.bot.views import files as file_views
from app.bot.views import home as home_views
from app.bot.views import settings as settings_views
from app.services import upload_filter, video_compress
from app.services.batch_download import BatchDownloadMode


@tb.auto_answer
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
            await tb.show_screen(query.message, tb.build_home_screen())

        elif data in ("nav:settings", "menu:settings", "menu:manga_settings"):
            context.user_data.pop("target_wait", None)
            await tb.show_screen(query.message, tb.build_settings_screen(user_id))

        elif data == "nav:upload_filter":
            await tb.show_screen(
                query.message, settings_views.upload_filter_screen(tb.get_user_settings(user_id))
            )

        elif data.startswith("uf:"):
            value = data.split(":", 1)[1]
            if value not in upload_filter.PRESETS:
                await tb.answer_once(query, "This filter no longer exists.", show_alert=True)
                return
            await tb.update_setting(user_id, "auto_upload_types", value)
            await tb.answer_once(query, f"Auto-upload filter: {upload_filter.label(value)}")
            await tb.show_screen(
                query.message, settings_views.upload_filter_screen(tb.get_user_settings(user_id))
            )

        elif data in ("nav:target", "tg:cancel"):
            context.user_data.pop("target_wait", None)
            await tb.show_screen(query.message, tb.build_target_screen(user_id))

        elif data == "tg:set":
            context.user_data["target_wait"] = time.time()
            await tb.show_screen(query.message, tb.build_target_screen(user_id, waiting=True))

        elif data == "tg:reset":
            await tb.update_setting(user_id, "target_chat_id", None)
            await tb.update_setting(user_id, "target_chat_title", "")
            await tb.answer_once(query, "Uploads go to Saved Messages again.")
            await tb.show_screen(query.message, tb.build_target_screen(user_id))

        elif data in ("nav:cookies", "ck:cancel"):
            context.user_data.pop("cookies_wait", None)
            await tb.show_screen(query.message, tb.build_cookies_screen(context))

        elif data == "ck:send":
            context.user_data["cookies_wait"] = time.time()
            await tb.show_screen(query.message, tb.build_cookies_screen(context))

        elif data == "ck:del":
            await tb.show_screen(
                query.message, tb.build_cookies_screen(context, confirm_delete=True)
            )

        elif data == "ck:dely":
            tb.UPLOADED_COOKIES_PATH.unlink(missing_ok=True)
            await tb.answer_once(query, "Cookies removed.")
            await tb.show_screen(query.message, tb.build_cookies_screen(context))

        elif data in ("nav:archive_settings", "menu:zip_settings", "zip_menu:settings"):
            if data == "zip_menu:settings" and "archive_settings_back" not in context.user_data:
                context.user_data["archive_settings_back"] = "nav:archive"
            elif data != "zip_menu:settings":
                context.user_data["archive_settings_back"] = "nav:settings"
            await tb.show_screen(query.message, tb.build_archive_settings_screen(user_id, context))

        elif data in ("nav:archive", "menu:zip"):
            context.user_data["archive_settings_back"] = "nav:archive"
            tb.pending_zip_name_sessions.pop(user_id, None)
            await tb.show_screen(query.message, await tb.build_archive_menu_screen(user_id))

        elif data == "nav:help":
            await tb.show_screen(query.message, home_views.help_screen(tb.SUPPORTED_SITES_URL))

        elif data.startswith("sites:"):
            raw = data.split(":", 1)[1]
            await tb.show_screen(
                query.message, tb.build_sites_screen(int(raw) if raw.isdigit() else 0)
            )

        elif data in ("nav:status", "menu:status"):
            await tb.show_status_dashboard(
                context.application, chat_id, user_id, replace=query.message
            )

        elif data.startswith("mcd:"):
            raw = data.split(":", 1)[1]
            if not raw.isdigit():
                await tb.answer_once(query, "This setting no longer exists.", show_alert=True)
                return
            value = tb.limit_of({"max_concurrent_downloads": int(raw)})
            await tb.update_setting(user_id, "max_concurrent_downloads", value)
            await tb.answer_once(query, f"Downloads at once: {value}")
            await tb.show_screen(query.message, tb.build_settings_screen(user_id))

        elif data.startswith("set:"):
            key = data.split(":", 1)[1]
            settings = tb.get_user_settings(user_id)
            if key in settings_views.HUB_TOGGLES:
                await tb.update_setting(user_id, key, not settings.get(key, False))
            elif key == "video_default":
                await tb.update_setting(
                    user_id, key, settings_views.next_video_default(settings.get(key))
                )
            elif key == "compress_crf":
                await tb.update_setting(user_id, key, video_compress.next_crf(settings.get(key)))
            elif key == "compress_preset":
                await tb.update_setting(user_id, key, video_compress.next_preset(settings.get(key)))
            elif key == "batch_download_mode":
                current = tb.normalize_batch_download_mode(settings.get(key))
                next_mode = (
                    BatchDownloadMode.DOWNLOAD_ONLY
                    if current is BatchDownloadMode.UPLOAD_AND_DELETE
                    else BatchDownloadMode.UPLOAD_AND_DELETE
                )
                await tb.update_setting(user_id, key, next_mode.value)
            else:
                await tb.answer_once(query, "This setting no longer exists.", show_alert=True)
                return
            await tb.show_screen(query.message, tb.build_settings_screen(user_id))

        elif data in (
            "menu:forwarded_posts",
            "menu:batch_mode",
            "menu:language",
        ) or data.startswith("set_lang:"):
            # Buttons from the old settings menu.
            if data == "menu:forwarded_posts":
                settings = tb.get_user_settings(user_id)
                await tb.update_setting(
                    user_id,
                    "auto_download_forwarded_posts",
                    not settings.get("auto_download_forwarded_posts"),
                )
            elif data.startswith(("menu:language", "set_lang:")):
                await tb.answer_once(query, "The bot is English-only now.", show_alert=True)
            await tb.show_screen(query.message, tb.build_settings_screen(user_id))

        elif data.startswith("up_cancel:"):
            if tb.cancel_upload(data.split(":", 1)[1]):
                await tb.answer_once(query, "Cancelling upload...")
            else:
                await tb.answer_once(query, "This upload already finished.", show_alert=True)

        elif data in ("refresh_status", "refresh_dashboard"):
            # Buttons from the old status and live-dashboard messages.
            await tb.show_status_dashboard(
                context.application, chat_id, user_id, replace=query.message
            )

        elif data == "menu:file_browser":
            await tb.show_folder(query.message, "", 0)

        elif data == "menu:clear":
            text, markup = tb.build_clear_jobs_prompt()
            await tb.safe_edit_message(query.message, text, markup)

        elif data in ("menu:tpb", "menu:rarbg", "menu:prowlarr", "menu:search"):
            # Buttons from older menus still in the chat history.
            provider_key = data.split(":", 1)[1]
            await tb.search_ui.open(
                context,
                query.message,
                None if provider_key == "search" else provider_key,
                edit=True,
            )

        elif data.startswith(("cancel_confirm:", "job_cancel:", "job_pause:", "job_resume:")):
            # cancel_confirm comes from the "cancel <n>" text command; the job_*
            # buttons from status messages sent before job cards existed.
            kind, _, raw_id = data.partition(":")
            jid = int(raw_id)
            action = {"job_pause": tb.pause_job, "job_resume": tb.resume_job}.get(
                kind, tb.cancel_job
            )
            ok, msg = await action(jid)
            await tb.answer_once(query, msg, show_alert=not ok)
            job = tb.download_jobs.get(jid)
            if ok and job is not None:
                await tb.refresh_job_card(context.application, job, force=True)
                if action is tb.cancel_job:
                    job["card_final"] = job.get("status")
            if kind == "cancel_confirm":
                await tb.safe_edit_message(query.message, f"{ICON_OK if ok else ICON_WARN} {msg}")
            else:
                await tb.update_status_message(context.application, chat_id, user_id)

        elif data.startswith("manga_setting:"):
            # Buttons from the old manga settings screen.
            key = {
                "auto_convert": "manga_auto_convert_pdf",
                "remove_images": "manga_remove_images_after_conversion",
            }.get(data.split(":", 1)[1])
            if key:
                settings = tb.get_user_settings(user_id)
                await tb.update_setting(user_id, key, not settings.get(key, False))
            await tb.show_screen(query.message, tb.build_settings_screen(user_id))

        elif data == "noop_close":
            await tb.safe_edit_message(query.message, "OK, nothing changed.")

        elif data == "clear_confirm":
            removed = tb.clear_finished_jobs()
            await tb.safe_edit_message(
                query.message, f"{ICON_OK} Cleared {removed} finished job(s)."
            )

        elif data == "clear_cancel":
            await tb.safe_edit_message(query.message, "Nothing was cleared.")

        # ===== Torrent Selection =====
        elif data in ("tconfirm", "tall", "tcancel", "tselall", "tselnone") or data.startswith(
            ("tsel:", "tpage:")
        ):
            session = tb.torrent_select_sessions.get(user_id)
            if session is None:
                await tb.answer_once(
                    query,
                    "This file list has expired. Send the .torrent file again.",
                    show_alert=True,
                )
                return

            if data == "tcancel":
                tb.torrent_select_sessions.pop(user_id, None)
                await tb.safe_edit_message(query.message, "Torrent download cancelled.")
                return

            if data in ("tconfirm", "tall"):
                if data == "tconfirm" and not session["selected"]:
                    await tb.answer_once(
                        query, tb.get_lang(user_id, "select_at_least"), show_alert=True
                    )
                    return
                tb.torrent_select_sessions.pop(user_id, None)
                source = session["torrent_path"]
                count = len(session["files"])
                if data == "tconfirm":
                    selected = sorted(session["selected"], key=int)
                    source += f" --select-file={','.join(selected)}"
                    count = len(selected)
                job = await tb.start_aria2_download(context.application, chat_id, source, user_id)
                job["note"] = f"{count} of {len(session['files'])} files selected"
                await tb.attach_job_card(context.application, job, message=query.message)
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

            await tb.safe_edit_message(
                query.message,
                tb.build_torrent_select_text(user_id),
                tb.build_torrent_select_keyboard(user_id, session["page"]),
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

            if action in (
                "st",
                "sp",
                "sall",
                "snone",
                "sup",
                "szip",
                "sdel",
                "sdelyes",
                "sdone",
                "bupload_empty",
            ):
                await tb.handle_file_selection(update, context, action, parts)
                return

            if action in (
                "list",
                "o",
                "file",
                "more",
                "send_yes",
                "send_folder_confirm",
                "send_folder_yes",
                "delete_confirm",
                "delete_yes",
                "deleteall_confirm",
                "deleteall_yes",
                "zipdir",
                "sel",
                "manga_pdf",
                "conv_menu",
                "thumb_send",
                "gifprev",
                "compress",
                "here",
                "zip1",
                "ren",
                "sort",
                "find",
            ):
                rel_path = tb.decode_path(encoded.split(":", 1)[0]) if encoded else ""

            if action == "list":
                context.user_data.pop("files_wait", None)
                await tb.show_folder(query.message, rel_path, page)

            elif action == "sort":
                tb.files_sort[chat_id] = file_views.next_sort(tb.files_sort.get(chat_id, "new"))
                await tb.show_folder(query.message, rel_path, page)

            elif action == "find":
                context.user_data["files_wait"] = {"kind": "find", "since": time.time()}
                await tb.show_screen(
                    query.message, file_views.find_prompt_screen(rel_path, page, tb.encode_path)
                )

            elif action == "here":
                if not tb.safe_join(tb.DOWNLOAD_DIR, rel_path).is_file():
                    raise FileNotFoundError(rel_path)
                await tb.answer_once(query, "Sending…")

                async def report_send_error(exc: BaseException, name=None):
                    name = name or tb.rel_name(rel_path)
                    await context.application.bot.send_message(
                        chat_id=chat_id,
                        text=f"{ICON_FAIL} Couldn't send {name}: {tb.shorten(str(exc), 300)}",
                    )

                tb.run_in_background(
                    tb.send_file_here(context.application, chat_id, rel_path),
                    name="send-here",
                    on_error=report_send_error,
                )

            elif action == "zip1":
                full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
                if not full.is_file():
                    raise FileNotFoundError(rel_path)
                await tb.ask_zip_name(
                    query.message,
                    user_id,
                    chat_id,
                    [full],
                    default_name=full.stem[:100] or tb.default_archive_name(),
                    cancel_data=f"fb:file:{page}:{encoded}",
                )

            elif action == "ren":
                if not tb.safe_join(tb.DOWNLOAD_DIR, rel_path).is_file():
                    raise FileNotFoundError(rel_path)
                context.user_data["files_wait"] = {
                    "kind": "rename",
                    "rel": rel_path,
                    "page": page,
                    "since": time.time(),
                }
                await tb.show_screen(
                    query.message, file_views.rename_prompt_screen(rel_path, page, tb.encode_path)
                )

            elif action == "o":
                full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
                if full.is_dir():
                    await tb.show_folder(query.message, rel_path, 0)
                elif full.is_file():
                    await tb.show_file(query.message, rel_path, page, user_id=user_id)
                else:
                    raise FileNotFoundError(rel_path)

            elif action == "file":
                context.user_data.pop("files_wait", None)
                await tb.show_file(query.message, rel_path, page, user_id=user_id)

            elif action == "more":
                info = await asyncio.to_thread(tb.folder_info, rel_path)
                await tb.show_screen(
                    query.message,
                    file_views.more_screen(
                        rel_path,
                        info,
                        page,
                        tb.encode_path,
                        is_manga=tb.is_manga_gallery_folder(
                            tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
                        ),
                    ),
                )

            elif action == "send_yes":
                if not tb.safe_join(tb.DOWNLOAD_DIR, rel_path).is_file():
                    raise FileNotFoundError(rel_path)
                await tb.answer_upload_tap(query)
                await tb.safe_edit_message(query.message, f"{ICON_UPLOAD} Preparing upload...")
                tb.run_in_background(
                    tb.send_single_file_via_pyrogram(
                        context.application,
                        query.message.chat_id,
                        query.message.message_id,
                        rel_path,
                        user_id=user_id,
                    ),
                    name="upload",
                    on_error=tb.message_error_reporter(
                        context.application,
                        query.message.chat_id,
                        query.message.message_id,
                        "Upload",
                    ),
                )

            elif action == "send_folder_confirm":
                info = await asyncio.to_thread(tb.folder_info, rel_path)
                if not info["file_count"]:
                    await tb.answer_once(
                        query, "There are no files in this folder.", show_alert=True
                    )
                    return
                await tb.safe_edit_message(
                    query.message,
                    f"{ICON_UPLOAD} Upload {info['file_count']} file(s) "
                    f"({tb.human_size(info['total_size'])}) from /{rel_path} to "
                    f"{tb.upload_target_label(tb.get_user_settings(user_id))}?",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    f"{ICON_OK} Yes, upload all",
                                    callback_data=f"fb:send_folder_yes:{page}:{encoded}",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    "✖ Cancel", callback_data=f"fb:more:{page}:{encoded}"
                                )
                            ],
                        ]
                    ),
                )

            elif action == "send_folder_yes":
                await tb.answer_upload_tap(query)
                await tb.safe_edit_message(
                    query.message, f"{ICON_UPLOAD} Preparing folder upload..."
                )
                tb.run_in_background(
                    tb.send_folder_files_via_pyrogram(
                        context.application,
                        query.message.chat_id,
                        query.message.message_id,
                        rel_path,
                        user_id=user_id,
                    ),
                    name="upload",
                    on_error=tb.message_error_reporter(
                        context.application,
                        query.message.chat_id,
                        query.message.message_id,
                        "Upload",
                    ),
                )

            elif action == "delete_confirm":
                full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
                if not rel_path or not full.exists():
                    raise FileNotFoundError(rel_path)
                info = await asyncio.to_thread(
                    tb.folder_info if full.is_dir() else tb.file_info, rel_path
                )
                await tb.show_screen(
                    query.message,
                    file_views.delete_confirm_screen(
                        rel_path, full.is_dir(), info, page, tb.encode_path
                    ),
                )

            elif action == "delete_yes":
                if not rel_path:
                    raise ValueError("Refusing to delete the download root")
                kind = await asyncio.to_thread(tb.delete_path, rel_path)
                await tb.answer_once(query, f"Deleted {kind}: {tb.rel_name(rel_path)}")
                await tb.show_folder(query.message, tb.rel_parent(rel_path), page)

            elif action == "deleteall_confirm":
                entries = await asyncio.to_thread(tb.list_dir, rel_path)
                if not entries:
                    await tb.answer_once(query, "This folder is already empty.", show_alert=True)
                    return
                info = await asyncio.to_thread(tb.folder_info, rel_path)
                await tb.safe_edit_message(
                    query.message,
                    f"{ICON_WARN} Permanently delete everything inside /{rel_path}?\n\n"
                    f"{info['file_count']} files in {info['folder_count']} folders · "
                    f"{tb.human_size(info['total_size'])}\n\nThis cannot be undone.",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    "🗑 Yes, delete everything",
                                    callback_data=f"fb:deleteall_yes:{page}:{encoded}",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    "✖ Cancel", callback_data=f"fb:more:{page}:{encoded}"
                                )
                            ],
                        ]
                    ),
                )

            elif action == "deleteall_yes":
                files_n, folders_n, errors = await asyncio.to_thread(
                    tb.delete_all_in_directory, rel_path
                )
                text = f"Deleted {files_n} file(s) and {folders_n} folder(s)."
                if errors:
                    text += f" {len(errors)} item(s) could not be deleted."
                await tb.answer_once(query, text, show_alert=bool(errors))
                await tb.show_folder(query.message, rel_path, 0)

            elif action == "zipdir":
                files = await asyncio.to_thread(tb.get_all_files_in_folder, rel_path)
                paths = tb.filter_files_for_archiving(
                    [tb.safe_join(tb.DOWNLOAD_DIR, f) for f in files]
                )
                if not paths:
                    await tb.answer_once(query, "There are no files to zip here.", show_alert=True)
                    return
                await tb.ask_zip_name(
                    query.message,
                    user_id,
                    chat_id,
                    paths,
                    default_name=tb.rel_name(rel_path) if rel_path else tb.default_archive_name(),
                    cancel_data=f"fb:more:{page}:{encoded}",
                )

            elif action == "sel":
                tb.batch_select_sessions[user_id] = {
                    "rel_path": rel_path,
                    "selected": set(),
                    "page": 0,
                }
                await tb.show_selection(query.message, user_id)

            elif action == "manga_pdf":
                folder = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
                if not tb.is_manga_gallery_folder(folder):
                    await tb.answer_once(
                        query, "No manga images found in this folder.", show_alert=True
                    )
                    return
                await tb.answer_once(query)
                await tb.safe_edit_message(
                    query.message, f"{ICON_IMAGE} Converting images to PDF...\n\n{folder.name}"
                )
                pdf_path = await tb.convert_manga_folder_to_pdf_job(folder, user_id)
                pdf_rel = tb.file_rel_path(pdf_path)
                await tb.safe_edit_message(
                    query.message,
                    f"{ICON_OK} PDF created\n\n{pdf_path.name}",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    f"{ICON_UPLOAD} Upload the PDF",
                                    callback_data=f"fb:send_yes:0:{tb.encode_path(pdf_rel)}",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    f"{ICON_FOLDER} Open its folder",
                                    callback_data=f"fb:list:0:{tb.encode_path(tb.rel_parent(pdf_rel))}",
                                )
                            ],
                        ]
                    ),
                )

            elif action == "conv_menu":
                await tb.safe_edit_message(
                    query.message,
                    f"🎬 Convert {tb.rel_name(rel_path)} to:",
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    res, callback_data=f"fb:conv_start:{page}:{encoded}:{res}"
                                )
                                for res in ("1080p", "720p")
                            ],
                            [
                                InlineKeyboardButton(
                                    res, callback_data=f"fb:conv_start:{page}:{encoded}:{res}"
                                )
                                for res in ("480p", "360p")
                            ],
                            [
                                InlineKeyboardButton(
                                    "✖ Cancel", callback_data=f"fb:file:{page}:{encoded}"
                                )
                            ],
                        ]
                    ),
                )

            elif action == "thumb_send":
                await tb.answer_once(query, "Making thumbnails…")
                await tb.send_thumbnail(update, context, rel_path)

            elif action == "gifprev":
                full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
                if not full.is_file():
                    raise FileNotFoundError(rel_path)
                await tb.answer_once(query, "Making GIF previews…")
                tb.run_in_background(
                    tb.send_gif_previews(context.application, chat_id, full), name="gif-previews"
                )

            elif action == "compress":
                full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
                if not full.is_file() or not tb.is_video_file(str(full)):
                    await tb.answer_once(
                        query, "This is not a video file (any more).", show_alert=True
                    )
                    return
                await tb.answer_once(query, "Compressing…")
                await tb.start_compress_job(
                    context.application,
                    chat_id,
                    user_id,
                    rel_path,
                    message=query.message,
                    back=f"fb:file:{page}:{encoded}",
                )

            elif action == "organize":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded = parts[3] if len(parts) > 3 else ""
                rel_path = tb.decode_path(encoded) if encoded else ""
                plan = await asyncio.to_thread(
                    tb.build_organize_plan, rel_path, tb.active_job_names()
                )
                text, markup = tb.build_organize_preview(rel_path, page, plan)
                await tb.safe_edit_message(query.message, text, markup)

            elif action == "organize_go":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                encoded, _, mode = (parts[3] if len(parts) > 3 else "").partition(":")
                rel_path = tb.decode_path(encoded) if encoded else ""
                await tb.safe_edit_message(query.message, f"{ICON_BROOM} Organizing videos...")
                # Re-plan so files that changed since the preview are handled correctly.
                plan = await asyncio.to_thread(
                    tb.build_organize_plan, rel_path, tb.active_job_names()
                )
                result = await asyncio.to_thread(
                    tb.apply_organize, plan, delete_leftovers=(mode == "purge")
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
                    lines.extend(f"• {tb.shorten(err, 80)}" for err in result.errors[:5])
                await tb.safe_edit_message(
                    query.message,
                    "\n".join(lines),
                    InlineKeyboardMarkup(
                        [
                            [
                                InlineKeyboardButton(
                                    f"{ICON_FOLDER} Open folder",
                                    callback_data=f"fb:list:{page}:{encoded}",
                                )
                            ]
                        ]
                    ),
                )

            elif action == "conv_start":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                # data.split(":", 3) keeps "<token>:<resolution>" together in parts[3].
                encoded, _, target_res = (parts[3] if len(parts) > 3 else "").partition(":")
                force = target_res.endswith("!")
                target_res = target_res.rstrip("!") or "720p"
                rel_path = tb.decode_path(encoded) if encoded else ""
                input_path = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
                if not input_path.exists() or not tb.is_video_file(str(input_path)):
                    await tb.answer_once(
                        query, "This is not a video file (any more).", show_alert=True
                    )
                    return
                await tb.answer_once(query)

                output_path = (
                    input_path.parent / f"{input_path.stem}_{target_res}{input_path.suffix}"
                )
                back_to_file = InlineKeyboardButton(
                    "🔙 Back to file", callback_data=f"fb:file:{page}:{encoded}"
                )
                if output_path.exists() and not force:
                    await tb.safe_edit_message(
                        query.message,
                        f"⏩ A {target_res} version already exists:\n{output_path.name}",
                        InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "📤 Upload it",
                                        callback_data=f"fb:conv_upload:{page}:{encoded}:{tb.encode_path(tb.file_rel_path(output_path))}",
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🔁 Convert again (replace it)",
                                        callback_data=f"fb:conv_start:{page}:{encoded}:{target_res}!",
                                    )
                                ],
                                [back_to_file],
                            ]
                        ),
                    )
                    return
                if force:
                    output_path.unlink(missing_ok=True)

                msg = query.message
                await tb.safe_edit_message(msg, f"🔄 Converting to {target_res}... 0%")

                async def run_conversion():
                    async def progress_callback(pct):
                        try:
                            await msg.edit_text(f"🔄 Converting to {target_res}... {pct}%")
                        except Exception as exc:
                            tb.logger.debug("Conversion progress edit failed: %s", exc)

                    await tb.convert_video_quality(
                        str(input_path), str(output_path), target_res, progress_callback
                    )
                    await msg.edit_text(
                        f"✅ Conversion finished!\nOutput: {output_path.name}\nSize: {tb.human_size(output_path.stat().st_size)}",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "📤 Upload converted file",
                                        callback_data=f"fb:conv_upload:{page}:{encoded}:{tb.encode_path(tb.file_rel_path(output_path))}",
                                    )
                                ],
                                [back_to_file],
                            ]
                        ),
                    )

                tb.run_in_background(
                    run_conversion(),
                    name="convert",
                    on_error=tb.message_error_reporter(
                        context.application, msg.chat_id, msg.message_id, "Conversion"
                    ),
                )

            elif action == "conv_upload":
                page = int(parts[2]) if len(parts) > 2 and parts[2] else 0
                original_encoded, _, converted_encoded = (
                    parts[3] if len(parts) > 3 else ""
                ).partition(":")
                converted_rel = tb.decode_path(converted_encoded) if converted_encoded else ""
                await tb.answer_upload_tap(query)
                await tb.safe_edit_message(
                    query.message, f"{ICON_UPLOAD} Uploading converted file..."
                )
                tb.run_in_background(
                    tb.send_single_file_via_pyrogram(
                        context.application,
                        query.message.chat_id,
                        query.message.message_id,
                        converted_rel,
                        user_id=user_id,
                    ),
                    name="upload",
                    on_error=tb.message_error_reporter(
                        context.application,
                        query.message.chat_id,
                        query.message.message_id,
                        "Upload",
                    ),
                )

            # New: Send thumbnail (multiple frames)

        # ===== Archive =====
        elif data == "zipname:default":
            session = tb.pending_zip_name_sessions.get(user_id)
            if session is None:
                await tb.answer_once(query, "This archive request has expired.", show_alert=True)
                return
            await tb.answer_once(query)
            tb.run_in_background(
                tb.run_named_zip(
                    context.application, user_id, session["default_name"], query.message
                ),
                name="zip",
                on_error=tb.message_error_reporter(
                    context.application, chat_id, query.message.message_id, "Archive"
                ),
            )

        elif data in ("zip_menu:zip_all",):
            all_files = await asyncio.to_thread(
                lambda: tb.filter_files_for_archiving(tb.collect_download_files())
            )
            if not all_files:
                await tb.answer_once(query, "There are no files to zip.", show_alert=True)
                return
            await tb.ask_zip_name(
                query.message,
                user_id,
                chat_id,
                all_files,
                default_name=tb.default_archive_name(),
                cancel_data="nav:archive",
            )

        elif data.startswith(("zip_menu:", "zip_select:")):
            # zip_menu:back/list/select and zip_select:* come from the old archive menu.
            if data.startswith(("zip_menu:select", "zip_select:")):
                tb.batch_select_sessions[user_id] = {"rel_path": "", "selected": set(), "page": 0}
                await tb.show_selection(query.message, user_id)
            else:
                await tb.show_screen(query.message, await tb.build_archive_menu_screen(user_id))

        # ===== Zip Settings =====
        elif data.startswith("zip_setting:"):
            setting_key = data.split(":")[1]
            back = "nav:archive_settings"

            if setting_key == "part_size":
                await tb.show_screen(
                    query.message,
                    settings_views.archive_choice_screen(
                        "Split archives into parts of",
                        [
                            ("256 MB", "zip_set_value:zip_part_size:268435456"),
                            ("512 MB", "zip_set_value:zip_part_size:536870912"),
                            ("1 GB", "zip_set_value:zip_part_size:1073741824"),
                            (
                                "2 GB (largest Telegram upload)",
                                "zip_set_value:zip_part_size:2147483648",
                            ),
                        ],
                        back,
                    ),
                )

            elif setting_key == "method":
                await tb.show_screen(
                    query.message,
                    settings_views.archive_choice_screen(
                        "Archive format",
                        [
                            ("ZIP — opens everywhere", "zip_set_value:zip_method:zip"),
                            ("7Z — smaller, needs 7-Zip", "zip_set_value:zip_method:7z"),
                        ],
                        back,
                    ),
                )

            elif setting_key == "compression":
                await tb.show_screen(
                    query.message,
                    settings_views.archive_choice_screen(
                        "Compression level (1 = fastest, 9 = smallest)",
                        [
                            (str(level), f"zip_set_value:compression_level:{level}")
                            for level in range(1, 10)
                        ],
                        back,
                    ),
                )

            elif setting_key == "password":
                session = tb.zip_select_sessions.get(user_id, {"selected": set(), "page": 0})
                session["waiting_for"] = "password"
                tb.zip_select_sessions[user_id] = session
                await tb.safe_edit_message(
                    query.message,
                    "🔐 Send the archive password as your next message.\n\n"
                    "Send none to remove the password, or /cancel to keep the current one.",
                )

            elif setting_key in (
                "auto_del_files",
                "auto_del_zips",
                "auto_del_upload",
                "forwarded_posts",
            ):
                setting_name = {
                    "auto_del_files": "auto_delete_files_after_zip",
                    "auto_del_zips": "auto_delete_zips_after_send",
                    "auto_del_upload": "auto_delete_files_after_upload",
                    "forwarded_posts": "auto_download_forwarded_posts",
                }[setting_key]
                settings = tb.get_user_settings(user_id)
                await tb.update_setting(
                    user_id, setting_name, not settings.get(setting_name, False)
                )
                await tb.show_screen(
                    query.message, tb.build_archive_settings_screen(user_id, context)
                )

            else:
                # e.g. batch_mode from the old screen; it lives in Settings now.
                await tb.show_screen(query.message, tb.build_settings_screen(user_id))

        elif data.startswith("zip_set_value:"):
            parts = data.split(":")
            setting_name = parts[1]
            value = ":".join(parts[2:])

            if setting_name in ("zip_part_size", "compression_level"):
                try:
                    value = int(value)
                except ValueError:
                    # FIX #7: Use translated string instead of hardcoded
                    await tb.answer_once(
                        query, tb.get_lang(user_id, "invalid_value"), show_alert=True
                    )
                    return
            elif setting_name == "zip_method":
                value = value.lower()
                if value not in ("zip", "7z"):
                    await tb.answer_once(
                        query, tb.get_lang(user_id, "invalid_archive_method"), show_alert=True
                    )
                    return
                fmt_err = tb.check_archive_format_support(value)
                if fmt_err:
                    await tb.answer_once(query, fmt_err, show_alert=True)
                    return
            elif setting_name in (
                "auto_delete_files_after_zip",
                "auto_delete_zips_after_send",
                "auto_delete_files_after_upload",
            ):
                value = value.lower() == "true"

            if setting_name == "zip_part_size" and not tb.validate_part_size(
                value // (1024 * 1024)
            ):
                await tb.answer_once(
                    query, tb.get_lang(user_id, "part_size_error"), show_alert=True
                )
                return
            elif setting_name == "compression_level" and not tb.validate_compression_level(value):
                # FIX #7: Use translated string instead of hardcoded
                await tb.answer_once(
                    query, tb.get_lang(user_id, "compression_error"), show_alert=True
                )
                return

            ok = await tb.update_setting(user_id, setting_name, value)
            if not ok:
                await tb.answer_once(query, tb.get_lang(user_id, "invalid_value"), show_alert=True)
                return

            await tb.show_screen(query.message, tb.build_archive_settings_screen(user_id, context))

    except Exception as e:
        if isinstance(e, RetryAfter):
            # Flood control: the screen can't change, but a pop-up still can.
            flood_guard.note(chat_id, tb._retry_seconds(e))
            tb.logger.warning("Button %r: chat %s is under flood control", data, chat_id)
            await tb.answer_once(query, tb.flood_blocked_alert(chat_id), show_alert=True)
            tb.run_in_background(tb.announce_flood_block(chat_id), name=f"flood-notice-{chat_id}")
            return
        if isinstance(e, NetworkError) and not isinstance(e, BadRequest):
            # The connection to Telegram dropped; the screen is unchanged, a new tap works.
            tb.logger.warning("Button %r failed: Telegram connection error: %s", data, e)
            await tb.answer_once(
                query, "Couldn't reach Telegram just now. Tap again.", show_alert=True
            )
            return
        tb.logger.exception("Button %r failed", data)
        if isinstance(e, (ValueError, FileNotFoundError, NotADirectoryError, IsADirectoryError)):
            # Usually an expired path token or a file deleted since the menu was drawn.
            text = "That item is gone or this menu is too old. Open Files again."
        else:
            text = f"Something went wrong: {tb.shorten(str(e), 150)}"
        # Prefer a pop-up so the current screen stays usable.
        if not await tb.answer_once(query, text, show_alert=True):
            await tb.safe_edit_message(
                query.message,
                f"{ICON_WARN} {text}",
                InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton(f"{ICON_FOLDER} Files", callback_data="fb:list:0:")],
                    ]
                ),
            )
