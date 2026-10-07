"""Unified torrent search UI: one message that moves between screens.

Flow: pick a source (skipped when only one is configured) -> type a query ->
results page with numbered buttons -> result details -> download / choose
files / magnet. Every screen has a way back, and the waiting-for-query state
expires on its own so stray messages are not swallowed as searches.

Callback data is ``srch:<action>[:<sid>[:<arg>...]]``. ``sid`` identifies the
search the buttons belong to, so buttons on an old results message say so
instead of acting on a newer search.
"""

from __future__ import annotations

import html
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

from app.bot.callbacks import answer_once, auto_answer
from app.services.torrent_search import SearchProvider, fetch_until

logger = logging.getLogger(__name__)

# start_download(update, context, source, force) -> job dict; a job with
# status "duplicate" means a file with that name already exists.
StartDownload = Callable[[Any, Any, str, bool], Awaitable[dict[str, Any]]]
SelectFiles = Callable[[Any, Any, Path, str], Awaitable[None]]

PER_PAGE = 6
WAIT_SECONDS = 600
SESSION_KEY = "search"
WAIT_KEY = "search_wait"


def _e(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _stats_line(item: dict[str, Any]) -> str:
    parts = [f"💾 {_e(item.get('size') or '?')}"]
    if item.get("seeders") is not None:
        parts.append(f"🟢 {item['seeders']}")
    if item.get("leechers") is not None:
        parts.append(f"🔴 {item['leechers']}")
    return " · ".join(parts)


class SearchUI:
    def __init__(
        self,
        providers: list[SearchProvider],
        start_download: StartDownload,
        select_files: SelectFiles,
        torrent_dir: Path,
    ):
        self.providers = {provider.key: provider for provider in providers}
        self.start_download = start_download
        self.select_files = select_files
        self.torrent_dir = torrent_dir

    # ------------------------------------------------------------ helpers

    @property
    def enabled(self) -> list[SearchProvider]:
        return [p for p in self.providers.values() if p.enabled]

    def _provider(self, key: str | None) -> SearchProvider | None:
        provider = self.providers.get(key or "")
        return provider if provider and provider.enabled else None

    @staticmethod
    async def _show(message: Any, text: str, markup: InlineKeyboardMarkup | None) -> Any:
        """Edit ``message`` in place; fall back to a new message if that fails."""
        try:
            return await message.edit_text(
                text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return message
            logger.debug("Search screen edit failed (%s); sending a new message", exc)
        return await message.reply_text(
            text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    @staticmethod
    def cancel_waiting(context: Any) -> None:
        context.user_data.pop(WAIT_KEY, None)

    @staticmethod
    def is_waiting(context: Any) -> bool:
        wait = context.user_data.get(WAIT_KEY)
        if not wait:
            return False
        if time.time() - wait.get("since", 0) > WAIT_SECONDS:
            context.user_data.pop(WAIT_KEY, None)
            return False
        return True

    def _session(self, context: Any, sid: str) -> dict[str, Any] | None:
        session: dict[str, Any] | None = context.user_data.get(SESSION_KEY)
        if not session or str(session.get("sid")) != sid:
            return None
        return session

    # ------------------------------------------------------------ screens

    def _picker(self) -> tuple[str, InlineKeyboardMarkup]:
        rows = [
            [InlineKeyboardButton(p.label, callback_data=f"srch:p:{p.key}")] for p in self.enabled
        ]
        rows.append([InlineKeyboardButton("✖ Close", callback_data="srch:x")])
        lines = ["🔍 <b>Search torrents</b>", "", "Choose where to search:"]
        lines += [f"• {_e(p.label)} — {_e(p.description)}" for p in self.enabled]
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def _prompt(self, provider: SearchProvider) -> tuple[str, InlineKeyboardMarkup]:
        buttons = []
        if len(self.enabled) > 1:
            buttons.append(InlineKeyboardButton("🔁 Change source", callback_data="srch:pick"))
        buttons.append(InlineKeyboardButton("✖ Cancel", callback_data="srch:x"))
        text = (
            f"🔍 <b>Search · {_e(provider.label)}</b>\n\n"
            "Send what you're looking for, for example <i>ubuntu 24.04</i>.\n"
            "Results cover every category; you can filter them afterwards."
        )
        return text, InlineKeyboardMarkup([buttons])

    def _results(self, session: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
        provider = self.providers[session["provider"]]
        items = session["items"]
        sid = session["sid"]
        category_label = provider.categories.get(session["category"], "🌐 All")
        header = (
            f"🔍 <b>“{_e(_short(session['query'], 60))}”</b> · "
            f"{_e(provider.label)} · {_e(category_label)}"
        )
        if not items:
            text = f"{header}\n\nNo results. Try other words or another category."
            rows = [
                [
                    InlineKeyboardButton("🏷 Category", callback_data=f"srch:cat:{sid}"),
                    InlineKeyboardButton("🔁 New search", callback_data="srch:new"),
                ],
                [InlineKeyboardButton("✖ Close", callback_data="srch:x")],
            ]
            return text, InlineKeyboardMarkup(rows)

        page = session["page"]
        start = page * PER_PAGE
        shown = items[start : start + PER_PAGE]
        more = "+" if not session["exhausted"] else ""
        lines = [
            header,
            f"Results {start + 1}–{start + len(shown)} of {len(items)}{more}",
            "",
        ]
        for offset, item in enumerate(shown):
            number = start + offset + 1
            source = f" · {_e(_short(item['source'], 24))}" if item.get("source") else ""
            lines.append(f"<b>{number}.</b> {_e(_short(item['title'], 90))}")
            lines.append(f"      {_stats_line(item)}{source}")
        lines += ["", "Tap a number for details and download."]

        number_buttons = [
            InlineKeyboardButton(str(start + i + 1), callback_data=f"srch:o:{sid}:{start + i}")
            for i in range(len(shown))
        ]
        rows = [number_buttons[i : i + 3] for i in range(0, len(number_buttons), 3)]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"srch:pg:{sid}:{page - 1}"))
        if start + PER_PAGE < len(items) or not session["exhausted"]:
            nav.append(InlineKeyboardButton("Next ▶", callback_data=f"srch:pg:{sid}:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append(
            [
                InlineKeyboardButton("🏷 Category", callback_data=f"srch:cat:{sid}"),
                InlineKeyboardButton("🔁 New search", callback_data="srch:new"),
            ]
        )
        rows.append([InlineKeyboardButton("✖ Close", callback_data="srch:x")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def _categories(self, session: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
        provider = self.providers[session["provider"]]
        sid = session["sid"]
        buttons = [
            InlineKeyboardButton(
                ("• " if key == session["category"] else "") + label,
                callback_data=f"srch:c:{sid}:{key}",
            )
            for key, label in provider.categories.items()
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton("⬅ Back to results", callback_data=f"srch:back:{sid}")])
        text = f"🏷 <b>Category</b> for “{_e(_short(session['query'], 60))}”"
        return text, InlineKeyboardMarkup(rows)

    def _detail(self, session: dict[str, Any], index: int) -> tuple[str, InlineKeyboardMarkup]:
        item = session["items"][index]
        sid = session["sid"]
        lines = [f"<b>{_e(item['title'])}</b>", "", _stats_line(item)]
        if item.get("source"):
            lines.append(f"📡 {_e(item['source'])}")
        if item.get("added"):
            lines.append(f"📅 {_e(item['added'])}")
        rows = [[InlineKeyboardButton("📥 Download", callback_data=f"srch:dl:{sid}:{index}")]]
        if item.get("can_select_files"):
            rows.append(
                [InlineKeyboardButton("☑️ Choose files", callback_data=f"srch:sel:{sid}:{index}")]
            )
        if item.get("magnet"):
            rows.append(
                [InlineKeyboardButton("🧲 Magnet link", callback_data=f"srch:mag:{sid}:{index}")]
            )
        rows.append([InlineKeyboardButton("⬅ Back to results", callback_data=f"srch:back:{sid}")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    # ------------------------------------------------------------ entry points

    async def open(
        self,
        context: Any,
        message: Any,
        provider_key: str | None = None,
        *,
        edit: bool,
    ) -> None:
        """Show the source picker, or the query prompt when the source is known."""
        if not self.enabled:
            text = (
                "🔍 No torrent search source is configured.\n"
                "Set PROWLARR_URL/PROWLARR_API_KEY, TPB_API_URL or RARBG_BASE_URL in .env."
            )
            markup = None
        else:
            provider = self._provider(provider_key)
            if provider is None and provider_key:
                text, markup = self._picker()
                text = f"⚠️ {_e(provider_key)} is not configured.\n\n" + text
            elif provider is None and len(self.enabled) > 1:
                text, markup = self._picker()
            else:
                provider = provider or self.enabled[0]
                context.user_data[WAIT_KEY] = {"provider": provider.key, "since": time.time()}
                text, markup = self._prompt(provider)
        if edit:
            await self._show(message, text, markup)
        else:
            await message.reply_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )

    async def command(self, update: Any, context: Any) -> None:
        """/search, /prowlarr, /tpb, /rarbg."""
        name = (update.message.text or "").split()[0].lstrip("/").split("@")[0].lower()
        provider_key = name if name in self.providers else None
        await self.open(context, update.message, provider_key, edit=False)

    async def handle_text(self, update: Any, context: Any) -> bool:
        """Run a search if we are waiting for a query. Returns True when consumed."""
        if not self.is_waiting(context):
            return False
        wait = context.user_data.pop(WAIT_KEY)
        provider = self._provider(wait.get("provider"))
        query = (update.message.text or "").strip()
        if provider is None or not query:
            return False

        previous = context.user_data.get(SESSION_KEY) or {}
        session: dict[str, Any] = {
            "sid": str(int(previous.get("sid", 0)) + 1),
            "provider": provider.key,
            "query": query[:200],
            "category": "all",
            "items": [],
            "next_page": 0,
            "exhausted": False,
            "page": 0,
        }
        context.user_data[SESSION_KEY] = session
        message = await update.message.reply_text(
            f"🔍 Searching “{_e(_short(query, 60))}” on {_e(provider.label)}…",
            parse_mode=ParseMode.HTML,
        )
        await self._run_search(message, session, context)
        return True

    async def _run_search(self, message: Any, session: dict[str, Any], context: Any = None) -> None:
        provider = self.providers[session["provider"]]
        try:
            await fetch_until(provider, session, PER_PAGE * (session["page"] + 1) + 1)
        except Exception as exc:
            if context is not None and context.user_data.get(SESSION_KEY) is not session:
                return  # superseded (new category or search) while fetching
            logger.warning("Search on %s failed: %s", provider.key, exc)
            await self._show(
                message,
                f"❌ Search on {_e(provider.label)} failed:\n{_e(_short(str(exc), 300))}",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("🔁 Try again", callback_data="srch:new"),
                            InlineKeyboardButton("✖ Close", callback_data="srch:x"),
                        ]
                    ]
                ),
            )
            return
        if context is not None and context.user_data.get(SESSION_KEY) is not session:
            return  # superseded while fetching; the newer search draws the screen
        max_page = max(0, (len(session["items"]) - 1) // PER_PAGE)
        session["page"] = min(session["page"], max_page)
        await self._show(message, *self._results(session))

    # ------------------------------------------------------------ callbacks

    @auto_answer
    async def on_callback(self, update: Any, context: Any) -> None:
        query = update.callback_query
        parts = query.data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        message = query.message

        if action == "x":
            self.cancel_waiting(context)
            await self._show(message, "🔍 Search closed.", None)
            return
        if action == "pick":
            self.cancel_waiting(context)
            await self._show(message, *self._picker())
            return
        if action == "p":
            await self.open(context, message, parts[2] if len(parts) > 2 else None, edit=True)
            return
        if action == "new":
            session = context.user_data.get(SESSION_KEY) or {}
            await self.open(context, message, session.get("provider"), edit=True)
            return

        sid = parts[2] if len(parts) > 2 else ""
        session = self._session(context, sid)
        if session is None:
            await answer_once(
                query, "These results are from an older search. Start a new one.", show_alert=True
            )
            return

        if action == "pg":
            await answer_once(query)
            session["page"] = max(0, int(parts[3]))
            await self._run_search(message, session, context)
        elif action == "back":
            await self._show(message, *self._results(session))
        elif action == "cat":
            await self._show(message, *self._categories(session))
        elif action == "c":
            await answer_once(query)
            # A fresh dict: a page fetch still running on the old one must not
            # add the previous category's results to this list.
            session = {
                **session,
                "category": parts[3],
                "items": [],
                "next_page": 0,
                "exhausted": False,
                "page": 0,
            }
            context.user_data[SESSION_KEY] = session
            await self._show(message, f"🔍 Searching “{_e(_short(session['query'], 60))}”…", None)
            await self._run_search(message, session, context)
        elif action in {"o", "dl", "sel", "mag"}:
            try:
                index = int(parts[3])
                session["items"][index]
            except (IndexError, ValueError):
                await answer_once(query, "That result is no longer available.", show_alert=True)
                return
            if action == "o":
                await self._show(message, *self._detail(session, index))
            elif action == "mag":
                await message.reply_text(
                    f"<code>{_e(session['items'][index]['magnet'])}</code>",
                    parse_mode=ParseMode.HTML,
                )
            elif action == "dl":
                await answer_once(query)
                await self._download(update, context, session, index, force=len(parts) > 4)
            else:
                await answer_once(query)
                await self._choose_files(update, context, session, index)

    async def _download(
        self,
        update: Any,
        context: Any,
        session: dict[str, Any],
        index: int,
        *,
        force: bool,
    ) -> None:
        provider = self.providers[session["provider"]]
        item = session["items"][index]
        sid = session["sid"]
        message = update.callback_query.message
        back = InlineKeyboardButton("⬅ Back to results", callback_data=f"srch:back:{sid}")
        await self._show(message, f"⏳ Starting <b>{_e(_short(item['title'], 80))}</b>…", None)
        try:
            resolved = await provider.resolve(item, self.torrent_dir)
            job = await self.start_download(update, context, resolved.source, force)
        except Exception as exc:
            logger.warning("Search download failed: %s", exc)
            await self._show(
                message,
                f"❌ Couldn't start the download:\n{_e(_short(str(exc), 300))}",
                InlineKeyboardMarkup([[back]]),
            )
            return

        if job.get("status") == "duplicate":
            await self._show(
                message,
                "⚠️ A file with this name is already in your downloads:\n"
                f"<b>{_e(job.get('name') or item['title'])}</b>",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "📥 Download anyway", callback_data=f"srch:dl:{sid}:{index}:f"
                            )
                        ],
                        [back],
                    ]
                ),
            )
            return
        await self._show(
            message,
            f"✅ <b>Download started</b> · Job #{job.get('id')}\n{_e(item['title'])}\n\n"
            "Follow it from 📊 Status.",
            InlineKeyboardMarkup([[back]]),
        )

    async def _choose_files(
        self,
        update: Any,
        context: Any,
        session: dict[str, Any],
        index: int,
    ) -> None:
        provider = self.providers[session["provider"]]
        item = session["items"][index]
        message = update.callback_query.message
        back = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅ Back to results", callback_data=f"srch:back:{session['sid']}"
                    )
                ]
            ]
        )
        await self._show(message, "⏳ Fetching the torrent's file list…", None)
        try:
            resolved = await provider.resolve(item, self.torrent_dir)
        except Exception as exc:
            await self._show(message, f"❌ {_e(_short(str(exc), 300))}", back)
            return
        if resolved.torrent_path is None:
            await self._show(
                message,
                "This result is a magnet link, so its file list isn't available before "
                "downloading. Use 📥 Download to get everything.",
                back,
            )
            return
        await self._show(message, f"☑️ Choose files for <b>{_e(item['title'])}</b> below.", back)
        await self.select_files(update, context, resolved.torrent_path, item["title"])
