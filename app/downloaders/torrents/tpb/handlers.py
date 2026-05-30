"""PTB handlers for The Pirate Bay crawler."""

import logging
from typing import Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.downloaders.torrents.tpb.crawler import TPBCrawler
from app.downloaders.torrents.tpb.formatting import format_tpb_detail, format_tpb_single
from app.downloaders.torrents.tpb.keyboards import (
    tpb_categories_keyboard,
    tpb_detail_keyboard,
    tpb_header_keyboard,
    tpb_result_keyboard,
)

logger = logging.getLogger(__name__)

PER_PAGE = 5


class TPBHandlers:
    """Container for all TPB PTB handlers."""

    def __init__(
        self,
        crawler: TPBCrawler,
        lang_func: Callable[[int, str], str],
        download_func=None,
    ):
        self.crawler = crawler
        self.lang = lang_func
        self.download_func = download_func

    def _lang(self, user_id: int, key: str) -> str:
        return self.lang(user_id, key)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def _cleanup_results(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        """Delete old result messages (keep header)."""
        msg_ids = context.user_data.pop("tpb_result_ids", [])
        for mid in msg_ids:
            try:
                await context.bot.delete_message(chat_id, mid)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def tpb_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tpb - start TPB search flow."""
        user_id = update.effective_user.id
        message = update.message

        context.user_data["tpb_waiting_for_query"] = True
        context.user_data.pop("tpb_query", None)
        context.user_data.pop("tpb_category", None)
        context.user_data.pop("tpb_page", None)
        await self._cleanup_results(context, message.chat.id)

        await message.reply_text(
            f"🏴‍☠️ <b>{self._lang(user_id, 'tpb_welcome')}</b>\n\n"
            f"{self._lang(user_id, 'tpb_send_query')}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        text=self._lang(user_id, 'home_btn'),
                        callback_data="menu_home",
                    ),
                ],
            ]),
            disable_web_page_preview=True,
        )

    async def tpb_get_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /tpbget_<id> - show torrent details."""
        user_id = update.effective_user.id
        text = update.message.text or ""

        try:
            torrent_id = text.split("_", 1)[1].split("@")[0]
        except IndexError:
            await update.message.reply_text(self._lang(user_id, "error_occurred"))
            return

        await self._show_detail(update, context, torrent_id)

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    async def category_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tpb_cat_<code>_<query>."""
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        try:
            _, category, search_query = query.data.split("_", 2)
        except ValueError:
            await query.edit_message_text(self._lang(user_id, "error_occurred"))
            return

        await self._cleanup_results(context, chat_id)
        await query.edit_message_text(
            f"⏳ {self._lang(user_id, 'searching_query').format(search_query)}"
        )

        await self._render_page(
            context,
            chat_id,
            user_id,
            query.message,
            search_query,
            category=category,
            page=0,
        )

    async def page_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tpb_page_<cat>_<query>_<page>."""
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        try:
            _, category, search_query, page_str = query.data.split("_", 3)
            page = int(page_str)
        except ValueError:
            await query.edit_message_text(self._lang(user_id, "error_occurred"))
            return

        await self._cleanup_results(context, chat_id)
        await self._render_page(
            context,
            chat_id,
            user_id,
            query.message,
            search_query,
            category=category,
            page=page,
        )

    async def newsearch_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tpb_newsearch - restart search flow."""
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        await self._cleanup_results(context, chat_id)
        context.user_data["tpb_waiting_for_query"] = True
        context.user_data.pop("tpb_query", None)
        context.user_data.pop("tpb_category", None)
        context.user_data.pop("tpb_page", None)

        await query.edit_message_text(
            f"🏴‍☠️ <b>{self._lang(user_id, 'tpb_welcome')}</b>\n\n"
            f"{self._lang(user_id, 'tpb_send_query')}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        text=self._lang(user_id, 'home_btn'),
                        callback_data="menu_home",
                    ),
                ],
            ]),
            disable_web_page_preview=True,
        )

    async def info_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tpb_info_<id> - show torrent details."""
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id

        try:
            torrent_id = query.data.split("_", 2)[2]
        except IndexError:
            await query.answer(self._lang(user_id, "error_occurred"), show_alert=True)
            return

        await self._show_detail(update, context, torrent_id)

    async def magnet_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tpb_magnet_<id> - send magnet as new message."""
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id

        try:
            torrent_id = query.data.split("_", 2)[2]
        except IndexError:
            await query.answer(self._lang(user_id, "error_occurred"), show_alert=True)
            return

        item = await self.crawler.get_torrent_details(torrent_id)
        if not item or not item.get("info_hash"):
            await query.answer(
                self._lang(user_id, "no_magnet_stored"),
                show_alert=True,
            )
            return

        magnet = TPBCrawler.build_magnet(
            item["info_hash"],
            item.get("name", "Unknown"),
        )

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"🧲 <code>{magnet}</code>",
        )

    async def download_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle tpb_dl_<id> - fetch magnet and start download."""
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id

        try:
            torrent_id = query.data.split("_", 2)[2]
        except IndexError:
            await query.answer(self._lang(user_id, "error_occurred"), show_alert=True)
            return

        await query.edit_message_reply_markup(reply_markup=None)

        item = await self.crawler.get_torrent_details(torrent_id)
        if not item or not item.get("info_hash"):
            await query.edit_message_text(
                f"❌ {self._lang(user_id, 'no_magnet_stored')}"
            )
            return

        magnet = TPBCrawler.build_magnet(
            item["info_hash"],
            item.get("name", "Unknown"),
        )

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
                        f"✅ {self._lang(user_id, 'tpb_download_started')}\n\n"
                        f"Job #{job['id']}\n"
                        f"{item.get('name', 'Unknown')}"
                    )
            except Exception as exc:
                logger.error("TPB download error: %s", exc)
                await query.edit_message_text(
                    f"❌ {self._lang(user_id, 'error_occurred')}: {exc}"
                )
        else:
            await query.edit_message_text(
                f"🧲 <code>{magnet}</code>\n\n"
                f"{self._lang(user_id, 'tpb_paste_to_download')}"
            )

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    async def _render_page(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        header_message,
        search_query: str,
        category: str = "0",
        page: int = 0,
    ):
        """Render a page of TPB results: edit header + send result messages."""
        all_results = await self.crawler.search(search_query, category=category)

        if not all_results:
            await header_message.edit_text(
                f"❌ {self._lang(user_id, 'no_results')}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        text="🔍",
                        callback_data="tpb_newsearch",
                    ),
                ]]),
            )
            return

        start = page * PER_PAGE
        end = start + PER_PAGE
        results = all_results[start:end]

        if not results and page > 0:
            page = 0
            start = 0
            end = PER_PAGE
            results = all_results[start:end]

        cat_name = {v: k for k, v in TPBCrawler.CATEGORIES.items()}.get(category, "all")
        cat_label = TPBCrawler.CATEGORY_LABELS.get(cat_name, "🌐 All")
        has_more = len(all_results) > end

        # Edit header
        await header_message.edit_text(
            f"🔍 <b>{search_query}</b>  ·  {cat_label}  ·  <i>Page {page + 1}</i>\n"
            f"<i>{self._lang(user_id, 'tpb_tap_download')}</i>",
            reply_markup=tpb_header_keyboard(
                search_query,
                category,
                page,
                has_more,
            ),
        )

        # Send result messages
        result_ids = []
        for idx, item in enumerate(results, start=start + 1):
            text = format_tpb_single(
                item,
                idx,
                lang_func=lambda k: self._lang(user_id, k),
            )
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=tpb_result_keyboard(item.get("id", "")),
                disable_web_page_preview=True,
            )
            result_ids.append(msg.message_id)

        context.user_data["tpb_result_ids"] = result_ids
        context.user_data["tpb_query"] = search_query
        context.user_data["tpb_category"] = category
        context.user_data["tpb_page"] = page

    # ------------------------------------------------------------------
    # Detail
    # ------------------------------------------------------------------

    async def _show_detail(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        torrent_id: str,
    ):
        """Fetch and show torrent details."""
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ {self._lang(user_id, 'tpb_fetching')}",
        )

        item = await self.crawler.get_torrent_details(torrent_id)
        if not item:
            await msg.edit_text(self._lang(user_id, "error_occurred"))
            return

        text = format_tpb_detail(
            item,
            lang_func=lambda k: self._lang(user_id, k),
        )

        await msg.edit_text(
            text,
            reply_markup=tpb_detail_keyboard(torrent_id),
            disable_web_page_preview=True,
        )

    # ------------------------------------------------------------------
    # Text helper
    # ------------------------------------------------------------------

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle text messages when user is in TPB search flow."""
        user_id = update.effective_user.id
        text = (update.message.text or "").strip()

        if not context.user_data.get("tpb_waiting_for_query"):
            return False

        context.user_data.pop("tpb_waiting_for_query", None)
        await self._cleanup_results(context, update.effective_chat.id)

        if not text:
            await update.message.reply_text(
                self._lang(user_id, "tpb_send_query"),
            )
            context.user_data["tpb_waiting_for_query"] = True
            return True

        await update.message.reply_text(
            f"🔍 {self._lang(user_id, 'select_category').format(text)}",
            reply_markup=tpb_categories_keyboard(text),
            disable_web_page_preview=True,
        )
        return True
