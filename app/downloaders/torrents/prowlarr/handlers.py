"""Telegram handlers for Prowlarr torrent search."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.downloaders.torrents.prowlarr.client import ProwlarrClient, ProwlarrConfigError
from app.downloaders.torrents.prowlarr.formatting import (
    format_prowlarr_detail,
    format_prowlarr_result,
)
from app.downloaders.torrents.prowlarr.keyboards import (
    prowlarr_categories_keyboard,
    prowlarr_header_keyboard,
    prowlarr_result_keyboard,
)

logger = logging.getLogger(__name__)


class ProwlarrHandlers:
    """Container for Prowlarr Telegram handlers."""

    def __init__(
        self,
        client: ProwlarrClient,
        lang_func: Callable[[int, str], str],
        download_func,
        select_torrent_func,
        torrent_dir: Path,
    ):
        self.client = client
        self.lang = lang_func
        self.download_func = download_func
        self.select_torrent_func = select_torrent_func
        self.torrent_dir = torrent_dir

    def _lang(self, user_id: int, key: str) -> str:
        return self.lang(user_id, key)

    async def _cleanup_results(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
        for message_id in context.user_data.pop("prowlarr_result_ids", []):
            with suppress(Exception):
                await context.bot.delete_message(chat_id, message_id)

    async def prowlarr_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        if not self.client.enabled:
            await update.message.reply_text(self._lang(user_id, "prowlarr_not_configured"))
            return
        context.user_data["prowlarr_waiting_for_query"] = True
        context.user_data.pop("prowlarr_results", None)
        await self._cleanup_results(context, update.effective_chat.id)
        await update.message.reply_text(
            f"🧭 <b>{self._lang(user_id, 'prowlarr_welcome')}</b>\n\n"
            f"{self._lang(user_id, 'prowlarr_send_query')}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            text=self._lang(user_id, "home_btn"), callback_data="menu_home"
                        )
                    ]
                ]
            ),
        )

    async def category_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        try:
            category, search_query = query.data.removeprefix("prowlarr_cat_").split("_", 1)
        except ValueError:
            await query.edit_message_text(self._lang(user_id, "error_occurred"))
            return
        await self._cleanup_results(context, chat_id)
        await query.edit_message_text(
            f"⏳ {self._lang(user_id, 'searching_query').format(search_query)}"
        )
        await self._render_results(context, chat_id, user_id, query.message, search_query, category)

    async def newsearch_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        await self._cleanup_results(context, update.effective_chat.id)
        context.user_data["prowlarr_waiting_for_query"] = True
        await query.edit_message_text(
            f"🧭 <b>{self._lang(user_id, 'prowlarr_welcome')}</b>\n\n"
            f"{self._lang(user_id, 'prowlarr_send_query')}"
        )

    async def info_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        release = self._release_from_callback(context, query.data, "prowlarr_info_")
        if not release:
            await query.answer("Search result expired. Run a new Prowlarr search.", show_alert=True)
            return
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=format_prowlarr_detail(release),
            disable_web_page_preview=True,
        )

    async def download_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        release = self._release_from_callback(context, query.data, "prowlarr_dl_")
        if not release:
            await query.answer("Search result expired. Run a new Prowlarr search.", show_alert=True)
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(f"⏳ Resolving {release.get('title', 'release')}...")
        try:
            source, _ = await self.client.resolve_download_source(release, self.torrent_dir)
            job = await self.download_func(update, context, source)
            await query.edit_message_text(
                f"✅ {self._lang(user_id, 'prowlarr_download_started')}\n\n"
                f"Job #{job['id']}\n{release.get('title', 'Unknown')}"
            )
        except Exception as exc:
            logger.error("Prowlarr download error: %s", exc)
            await query.edit_message_text(f"❌ {self._lang(user_id, 'error_occurred')}: {exc}")

    async def select_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        release = self._release_from_callback(context, query.data, "prowlarr_select_")
        if not release:
            await query.answer("Search result expired. Run a new Prowlarr search.", show_alert=True)
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(
            f"⏳ Fetching torrent file for selection...\n{release.get('title', 'Unknown')}"
        )
        try:
            source, torrent_path = await self.client.resolve_download_source(
                release, self.torrent_dir
            )
            if not torrent_path:
                await query.edit_message_text(
                    "⚠️ This Prowlarr result resolved to a magnet link. "
                    "File selection is available when the indexer returns a .torrent file."
                )
                return
            await self.select_torrent_func(
                update, context, torrent_path, release.get("title") or torrent_path.name
            )
        except Exception as exc:
            logger.error("Prowlarr select error: %s", exc)
            await query.edit_message_text(f"❌ {self._lang(user_id, 'error_occurred')}: {exc}")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        user_id = update.effective_user.id
        text = (update.message.text or "").strip()
        if not context.user_data.get("prowlarr_waiting_for_query"):
            return False
        context.user_data.pop("prowlarr_waiting_for_query", None)
        if not text:
            await update.message.reply_text(self._lang(user_id, "prowlarr_send_query"))
            context.user_data["prowlarr_waiting_for_query"] = True
            return True
        await update.message.reply_text(
            f"🔍 {self._lang(user_id, 'select_category').format(text)}",
            reply_markup=prowlarr_categories_keyboard(text),
            disable_web_page_preview=True,
        )
        return True

    async def _render_results(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        header_message,
        search_query: str,
        category: str,
    ) -> None:
        try:
            results = await self.client.search(search_query, category)
        except ProwlarrConfigError as exc:
            await header_message.edit_text(str(exc))
            return
        except Exception as exc:
            logger.error("Prowlarr search error: %s", exc)
            await header_message.edit_text(f"❌ {self._lang(user_id, 'error_occurred')}: {exc}")
            return
        if not results:
            await header_message.edit_text(
                f"❌ {self._lang(user_id, 'no_results')}",
                reply_markup=prowlarr_header_keyboard(),
            )
            return

        result_map = {item["token"]: item for item in results}
        context.user_data["prowlarr_results"] = result_map
        label = ProwlarrClient.CATEGORY_LABELS.get(category, category)
        await header_message.edit_text(
            f"🧭 <b>Prowlarr</b> · <b>{search_query}</b> · {label}\n"
            f"<i>{self._lang(user_id, 'prowlarr_tap_download')}</i>",
            reply_markup=prowlarr_header_keyboard(),
        )
        message_ids = []
        for index, item in enumerate(results[:10], start=1):
            message = await context.bot.send_message(
                chat_id=chat_id,
                text=format_prowlarr_result(item, index),
                reply_markup=prowlarr_result_keyboard(item["token"]),
                disable_web_page_preview=True,
            )
            message_ids.append(message.message_id)
        context.user_data["prowlarr_result_ids"] = message_ids

    @staticmethod
    def _release_from_callback(
        context: ContextTypes.DEFAULT_TYPE,
        data: str,
        prefix: str,
    ) -> dict | None:
        token = data.removeprefix(prefix)
        results = context.user_data.get("prowlarr_results", {})
        release = results.get(token)
        return release if isinstance(release, dict) else None
