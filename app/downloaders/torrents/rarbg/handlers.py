"""PTB handlers for the RARBG-style crawler."""

import logging
from collections.abc import Callable
from contextlib import suppress
from urllib.parse import unquote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.downloaders.torrents.rarbg.crawler import RARBGCrawler, RARBGVerificationError
from app.downloaders.torrents.rarbg.formatting import format_rarbg_detail, format_rarbg_single
from app.downloaders.torrents.rarbg.keyboards import (
    rarbg_categories_keyboard,
    rarbg_detail_keyboard,
    rarbg_header_keyboard,
    rarbg_result_keyboard,
)

logger = logging.getLogger(__name__)

PER_PAGE = 5


class RARBGHandlers:
    """Container for all RARBG-style PTB handlers."""

    def __init__(
        self,
        crawler: RARBGCrawler,
        lang_func: Callable[[int, str], str],
        download_func=None,
    ):
        self.crawler = crawler
        self.lang = lang_func
        self.download_func = download_func

    def _lang(self, user_id: int, key: str) -> str:
        return self.lang(user_id, key)

    async def _cleanup_results(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        msg_ids = context.user_data.pop("rarbg_result_ids", [])
        context.user_data.pop("rarbg_result_map", None)
        for mid in msg_ids:
            with suppress(Exception):
                await context.bot.delete_message(chat_id, mid)

    def _resolve_torrent_id(self, context: ContextTypes.DEFAULT_TYPE, token: str) -> str:
        result_map = context.user_data.get("rarbg_result_map", {})
        return result_map.get(token, token)

    async def rarbg_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        message = update.message

        context.user_data["rarbg_waiting_for_query"] = True
        context.user_data.pop("rarbg_query", None)
        context.user_data.pop("rarbg_category", None)
        context.user_data.pop("rarbg_page", None)
        await self._cleanup_results(context, message.chat.id)

        await message.reply_text(
            f"🧲 <b>{self._lang(user_id, 'rarbg_welcome')}</b>\n\n"
            f"{self._lang(user_id, 'rarbg_send_query')}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text=self._lang(user_id, "home_btn"), callback_data="menu_home"
                        )
                    ]
                ]
            ),
            disable_web_page_preview=True,
        )

    async def rarbg_get_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        text = update.message.text or ""
        try:
            torrent_id = text.split("_", 1)[1].split("@")[0]
        except IndexError:
            await update.message.reply_text(self._lang(user_id, "error_occurred"))
            return
        await self._show_detail(update, context, torrent_id)

    async def category_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        try:
            category, search_query = query.data.removeprefix("rarbg_cat_").split("_", 1)
        except ValueError:
            await query.edit_message_text(self._lang(user_id, "error_occurred"))
            return

        category = "" if category == "all" else category
        search_query = unquote(search_query)
        await self._cleanup_results(context, chat_id)
        await query.edit_message_text(
            f"⏳ {self._lang(user_id, 'searching_query').format(search_query)}"
        )
        await self._render_page(
            context, chat_id, user_id, query.message, search_query, category=category
        )

    async def page_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        try:
            category, rest = query.data.removeprefix("rarbg_page_").split("_", 1)
            search_query, page_str = rest.rsplit("_", 1)
            page = int(page_str)
        except ValueError:
            await query.edit_message_text(self._lang(user_id, "error_occurred"))
            return

        category = "" if category == "all" else category
        search_query = unquote(search_query)
        await self._cleanup_results(context, chat_id)
        await self._render_page(
            context, chat_id, user_id, query.message, search_query, category, page
        )

    async def newsearch_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        await self._cleanup_results(context, chat_id)
        context.user_data["rarbg_waiting_for_query"] = True
        context.user_data.pop("rarbg_query", None)
        context.user_data.pop("rarbg_category", None)
        context.user_data.pop("rarbg_page", None)

        await query.edit_message_text(
            f"🧲 <b>{self._lang(user_id, 'rarbg_welcome')}</b>\n\n"
            f"{self._lang(user_id, 'rarbg_send_query')}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text=self._lang(user_id, "home_btn"), callback_data="menu_home"
                        )
                    ]
                ]
            ),
            disable_web_page_preview=True,
        )

    async def info_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id

        try:
            torrent_id = self._resolve_torrent_id(context, query.data.split("_", 2)[2])
        except IndexError:
            await query.answer(self._lang(user_id, "error_occurred"), show_alert=True)
            return

        await self._show_detail(update, context, torrent_id)

    async def magnet_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id

        try:
            torrent_id = self._resolve_torrent_id(context, query.data.split("_", 2)[2])
        except IndexError:
            await query.answer(self._lang(user_id, "error_occurred"), show_alert=True)
            return

        try:
            item = await self.crawler.get_torrent_details(torrent_id)
        except RARBGVerificationError as exc:
            await query.answer(str(exc), show_alert=True)
            return

        magnet = item.get("magnet", "") if item else ""
        if not magnet:
            await query.answer(self._lang(user_id, "no_magnet_stored"), show_alert=True)
            return

        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=f"🧲 <code>{magnet}</code>"
        )

    async def download_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id

        try:
            torrent_id = self._resolve_torrent_id(context, query.data.split("_", 2)[2])
        except IndexError:
            await query.answer(self._lang(user_id, "error_occurred"), show_alert=True)
            return

        await query.edit_message_reply_markup(reply_markup=None)

        try:
            item = await self.crawler.get_torrent_details(torrent_id)
        except RARBGVerificationError as exc:
            await query.edit_message_text(f"⚠️ {exc}")
            return

        magnet = item.get("magnet", "") if item else ""
        if not magnet:
            await query.edit_message_text(f"❌ {self._lang(user_id, 'no_magnet_stored')}")
            return

        if self.download_func:
            try:
                job = await self.download_func(update, context, magnet)
                if job.get("status") == "duplicate":
                    await query.edit_message_text(
                        f"⚠️ {item.get('name', 'Unknown')}\n"
                        f"{self._lang(user_id, 'duplicate_detected')} {item.get('name', '')}"
                    )
                else:
                    await query.edit_message_text(
                        f"✅ {self._lang(user_id, 'rarbg_download_started')}\n\n"
                        f"Job #{job['id']}\n{item.get('name', 'Unknown')}"
                    )
            except Exception as exc:
                logger.error("RARBG download error: %s", exc)
                await query.edit_message_text(f"❌ {self._lang(user_id, 'error_occurred')}: {exc}")
        else:
            await query.edit_message_text(
                f"🧲 <code>{magnet}</code>\n\n{self._lang(user_id, 'rarbg_paste_to_download')}"
            )

    async def _render_page(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        header_message,
        search_query: str,
        category: str = "",
        page: int = 0,
    ):
        try:
            all_results = await self.crawler.search(search_query, category=category, page=page)
        except RARBGVerificationError as exc:
            await header_message.edit_text(f"⚠️ {exc}")
            return

        if not all_results:
            await header_message.edit_text(
                f"❌ {self._lang(user_id, 'no_results')}",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(text="🔍", callback_data="rarbg_newsearch")]]
                ),
            )
            return

        results = all_results[:PER_PAGE]
        cat_name = {v: k for k, v in RARBGCrawler.CATEGORIES.items()}.get(category, "all")
        cat_label = RARBGCrawler.CATEGORY_LABELS.get(cat_name, "🌐 All")
        has_more = len(all_results) >= PER_PAGE

        await header_message.edit_text(
            f"🔍 <b>{search_query}</b>  ·  {cat_label}  ·  <i>Page {page + 1}</i>\n"
            f"<i>{self._lang(user_id, 'rarbg_tap_download')}</i>",
            reply_markup=rarbg_header_keyboard(search_query, category, page, has_more),
        )

        result_ids = []
        result_map = {}
        for local_idx, item in enumerate(results):
            idx = page * PER_PAGE + local_idx + 1
            token = f"{page}-{local_idx}"
            result_map[token] = item.get("id", "")
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=format_rarbg_single(item, idx, lang_func=lambda k: self._lang(user_id, k)),
                reply_markup=rarbg_result_keyboard(token),
                disable_web_page_preview=True,
            )
            result_ids.append(msg.message_id)

        context.user_data["rarbg_result_ids"] = result_ids
        context.user_data["rarbg_result_map"] = result_map
        context.user_data["rarbg_query"] = search_query
        context.user_data["rarbg_category"] = category
        context.user_data["rarbg_page"] = page

    async def _show_detail(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        torrent_id: str,
    ):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        msg = await context.bot.send_message(
            chat_id=chat_id, text=f"⏳ {self._lang(user_id, 'rarbg_fetching')}"
        )
        try:
            item = await self.crawler.get_torrent_details(torrent_id)
        except RARBGVerificationError as exc:
            await msg.edit_text(f"⚠️ {exc}")
            return

        if not item:
            await msg.edit_text(self._lang(user_id, "error_occurred"))
            return

        await msg.edit_text(
            format_rarbg_detail(item, lang_func=lambda k: self._lang(user_id, k)),
            reply_markup=rarbg_detail_keyboard(torrent_id),
            disable_web_page_preview=True,
        )

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        user_id = update.effective_user.id
        text = (update.message.text or "").strip()

        if not context.user_data.get("rarbg_waiting_for_query"):
            return False

        context.user_data.pop("rarbg_waiting_for_query", None)
        await self._cleanup_results(context, update.effective_chat.id)

        if not text:
            await update.message.reply_text(self._lang(user_id, "rarbg_send_query"))
            context.user_data["rarbg_waiting_for_query"] = True
            return True

        await update.message.reply_text(
            f"🔍 {self._lang(user_id, 'select_category').format(text)}",
            reply_markup=rarbg_categories_keyboard(text),
            disable_web_page_preview=True,
        )
        return True
