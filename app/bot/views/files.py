"""File browser screens.

Every file and folder is its own button (no numbers to look up). The root
opens on 🕘 Recent (the latest downloads) above the folders; tapping a file
opens its action card.

Entries are dicts with ``name``, ``rel_path``, ``is_dir``, ``size``,
``mtime`` and, for folders, ``count`` (visible items inside). ``tok`` turns a
relative path into the short token used in callback data (Telegram caps
callback data at 64 bytes).

Callback data (``fb:<action>:<page>:<token>``, page = the folder page to
return to):
  list  open a folder page         o     open an entry (folder or file)
  file  file card                  more  folder actions
  here  send the file to this chat send_yes  upload to Saved Messages
  zip1  zip one file               ren   rename (asks for the new name)
  sort  cycle the sort order       find  look for files by name
  send_folder_confirm, delete_confirm, deleteall_confirm, organize,
  manga_pdf, conv_menu, thumb_send
Selection mode (session kept per user): sel, st:<token>, sp:<page>, sall,
snone, sup, szip, sdel, sdelyes, sdone.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardMarkup

from app.bot.views.common import (
    Screen,
    back_row,
    button,
    e,
    home_button,
    human_size,
    short,
)

PER_PAGE = 8
RECENT_COUNT = 3
BUTTON_NAME_LIMIT = 36

Tok = Callable[[str], str]

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".3gp"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}

SORTS = {"new": "🕘 Newest", "name": "🔤 Name", "size": "📏 Size"}

# yt-dlp names X/Twitter posts after the tweet text, links included (with "⧸"
# for "/"), and appends " [<video id>]". Neither helps anyone find the file.
_URL_RE = re.compile(r"https?:[/⧸]{2}\S+")
_ID_SUFFIX_RE = re.compile(r"\s*\[[\w-]{6,}\]$")


def icon(name: str, is_dir: bool) -> str:
    if is_dir:
        return "📁"
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXTS:
        return "🎬"
    if ext in AUDIO_EXTS:
        return "🎵"
    if ext in IMAGE_EXTS:
        return "🖼"
    if ext in ARCHIVE_EXTS:
        return "🗜"
    if ext == ".torrent":
        return "🧲"
    if ext == ".pdf":
        return "📕"
    return "📄"


def display_name(name: str, is_dir: bool = False) -> str:
    """A tidy name for buttons: no links, no yt-dlp id suffix."""
    path = Path(name)
    stem, ext = (name, "") if is_dir or not path.suffix else (path.stem, path.suffix)
    stem = _ID_SUFFIX_RE.sub("", _URL_RE.sub("", stem))
    stem = re.sub(r"\s+", " ", stem).strip(" -–—_·")
    return stem + ext if stem else name


def _isolate(text: str) -> str:
    # First-strong isolate: a Persian/Arabic name doesn't flip the "· size" after it.
    return f"\u2068{text}\u2069"


def _distinct(entries: list[dict[str, Any]]) -> dict[str, str]:
    """rel_path -> tidy name; names that tidy to the same thing get told apart."""
    tidy = {e["rel_path"]: display_name(e["name"], e["is_dir"]) for e in entries}
    counts: dict[str, int] = {}
    for name in tidy.values():
        counts[name] = counts.get(name, 0) + 1
    for entry in entries:
        name = tidy[entry["rel_path"]]
        if counts[name] > 1:
            match = re.search(r"\[([\w-]{6,})\]", entry["name"])
            tag = f"#{match.group(1)[-5:]}" if match else when(entry.get("mtime") or 0)
            path = Path(name)
            tidy[entry["rel_path"]] = (
                f"{path.stem} {tag}{path.suffix}" if not entry["is_dir"] else f"{name} {tag}"
            )
    return tidy


def entry_label(entry: dict[str, Any], mark: str = "", tidy: str | None = None) -> str:
    name = short(tidy or display_name(entry["name"], entry["is_dir"]), BUTTON_NAME_LIMIT)
    if entry["is_dir"]:
        detail = f"{entry.get('count', 0)} items"
    else:
        detail = human_size(entry.get("size", 0))
    return f"{mark}{icon(entry['name'], entry['is_dir'])} {_isolate(name)} · {detail}"


def breadcrumb(rel_path: str) -> str:
    parts = [part for part in rel_path.strip("/").split("/") if part]
    return " › ".join(["Files", *(short(display_name(p, True), 24) for p in parts)])


def shown_path(rel_path: str) -> str:
    return "/" + rel_path.strip("/") if rel_path else "/"


def parent_of(rel_path: str) -> str:
    parent = str(Path(rel_path).parent) if rel_path else ""
    return "" if parent == "." else parent


def page_count(total: int) -> int:
    return max(1, (total + PER_PAGE - 1) // PER_PAGE)


def clamp_page(page: int, total: int) -> int:
    return max(0, min(page, page_count(total) - 1))


def sort_entries(entries: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    """Folders first, then files by the chosen order."""
    if order == "name":
        key: Callable[[dict[str, Any]], Any] = lambda x: x["name"].lower()  # noqa: E731
        return sorted(entries, key=lambda x: (not x["is_dir"], key(x)))
    if order == "size":
        return sorted(entries, key=lambda x: (not x["is_dir"], -int(x.get("size") or 0)))
    return sorted(entries, key=lambda x: (not x["is_dir"], -float(x.get("mtime") or 0)))


def next_sort(order: str) -> str:
    keys = list(SORTS)
    return keys[(keys.index(order) + 1) % len(keys)] if order in SORTS else "name"


def when(mtime: float, now: float | None = None) -> str:
    moment = datetime.fromtimestamp(mtime)
    today = datetime.fromtimestamp(now or time.time()).date()
    days = (today - moment.date()).days
    if days == 0:
        return f"today {moment:%H:%M}"
    if days == 1:
        return f"yesterday {moment:%H:%M}"
    return f"{moment:%Y-%m-%d}"


def _nav_row(page: int, total: int, data_for_page: Callable[[int], str]) -> list[Any] | None:
    pages = page_count(total)
    if pages <= 1:
        return None
    row = []
    if page > 0:
        row.append(button("◀ Prev", data_for_page(page - 1)))
    if page < pages - 1:
        row.append(button("Next ▶", data_for_page(page + 1)))
    return row


def browser_screen(
    rel_path: str,
    entries: list[dict[str, Any]],
    page: int,
    tok: Tok,
    *,
    recent: list[dict[str, Any]] | None = None,
    free_bytes: int | None = None,
    order: str = "new",
) -> Screen:
    page = clamp_page(page, len(entries))
    here = tok(rel_path)
    pages = page_count(len(entries))
    shown = entries[page * PER_PAGE : (page + 1) * PER_PAGE]

    title = f"📁 <b>{e(breadcrumb(rel_path))}</b>"
    if not rel_path and free_bytes is not None:
        title += f" · 💽 {human_size(free_bytes)} free"
    lines = [title]
    if entries:
        lines.append(f"{len(entries)} items" + (f" · page {page + 1}/{pages}" if pages > 1 else ""))
    elif not recent:
        lines += [
            "",
            (
                "Nothing here yet. Send a link to download something."
                if not rel_path
                else "This folder is empty."
            ),
        ]

    names = _distinct([*(recent or []), *shown])
    rows = []
    if recent and page == 0:
        rows.append([button("🕘 Recent downloads", "noop")])
        rows += [
            [
                button(
                    entry_label(entry, tidy=names[entry["rel_path"]]),
                    f"fb:o:0:{tok(entry['rel_path'])}",
                )
            ]
            for entry in recent
        ]
        if shown:
            rows.append([button("📂 All files", "noop")])
    rows += [
        [
            button(
                entry_label(entry, tidy=names[entry["rel_path"]]),
                f"fb:o:{page}:{tok(entry['rel_path'])}",
            )
        ]
        for entry in shown
    ]
    nav = _nav_row(page, len(entries), lambda p: f"fb:list:{p}:{here}")
    if nav:
        rows.append(nav)
    if entries:
        rows.append(
            [
                button("🔍 Find", f"fb:find:{page}:{here}"),
                button("☑️ Select", f"fb:sel:{page}:{here}"),
                button(f"⇅ {SORTS.get(order, SORTS['new'])}", f"fb:sort:{page}:{here}"),
            ]
        )
    bottom = [button("⬆ Up", f"fb:list:0:{tok(parent_of(rel_path))}")] if rel_path else []
    if entries:
        bottom.append(button("⋯ More", f"fb:more:{page}:{here}"))
    bottom.append(home_button())
    rows.append(bottom)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def find_prompt_screen(rel_path: str, page: int, tok: Tok) -> Screen:
    text = (
        "🔍 <b>Find files</b>\n\n"
        "Send part of a name, for example <i>ubuntu</i> or <i>.mp3</i>. "
        "Every folder is searched."
    )
    return text, InlineKeyboardMarkup([[button("✖ Cancel", f"fb:list:{page}:{tok(rel_path)}")]])


def find_results_screen(query: str, results: list[dict[str, Any]], tok: Tok) -> Screen:
    if not results:
        text = f"🔍 Nothing matches “{e(short(query, 60))}”."
    else:
        more = "+" if len(results) >= 30 else ""
        text = f"🔍 <b>{len(results)}{more} matches for “{e(short(query, 60))}”</b>"
    names = _distinct(results)
    rows = [
        [button(entry_label(e_, tidy=names[e_["rel_path"]]), f"fb:o:0:{tok(e_['rel_path'])}")]
        for e_ in results
    ]
    rows.append([button("🔍 Search again", "fb:find:0:"), button("📁 Files", "fb:list:0:")])
    return text, InlineKeyboardMarkup(rows)


def more_screen(
    rel_path: str, info: dict[str, Any], page: int, tok: Tok, *, is_manga: bool
) -> Screen:
    here = tok(rel_path)
    text = (
        f"⋯ <b>{e(shown_path(rel_path))}</b>\n\n"
        f"{info['file_count']} files in {info['folder_count']} sub-folders · "
        f"{human_size(info['total_size'])}"
    )
    rows = [
        [button("📤 Upload every file here", f"fb:send_folder_confirm:{page}:{here}")],
        [button("📦 Zip this folder", f"fb:zipdir:{page}:{here}")],
        [button("🧹 Organize videos", f"fb:organize:{page}:{here}")],
    ]
    if is_manga:
        rows.append([button("📕 Convert images to PDF", f"fb:manga_pdf:{page}:{here}")])
    if rel_path:
        rows.append([button("🗑 Delete this folder", f"fb:delete_confirm:{page}:{here}")])
    rows.append([button("🗑 Delete everything inside", f"fb:deleteall_confirm:{page}:{here}")])
    rows.append(back_row(f"fb:list:{page}:{here}"))
    return text, InlineKeyboardMarkup(rows)


def file_screen(
    rel_path: str,
    size: int,
    mtime: float,
    page: int,
    tok: Tok,
    *,
    is_video: bool,
    can_send_here: bool = True,
    has_saved_messages: bool = True,
    send_limit: int = 50 * 1024 * 1024,
) -> Screen:
    me = tok(rel_path)
    name = Path(rel_path).name
    tidy = display_name(name)
    lines = [
        f"{icon(name, False)} <b>{e(tidy)}</b>",
        f"{human_size(size)} · {when(mtime)}",
        f"📁 {e(breadcrumb(parent_of(rel_path)))}",
    ]
    if tidy != name:
        lines.append(f"<code>{e(name)}</code>")
    if not can_send_here:
        lines += ["", f"Too big to send to this chat (over {human_size(send_limit)})."]
    rows = []
    send = []
    if can_send_here:
        send.append(button("📨 Send here", f"fb:here:{page}:{me}"))
    if has_saved_messages:
        send.append(button("📤 Saved Messages", f"fb:send_yes:{page}:{me}"))
    if send:
        rows.append(send)
    rows.append(
        [
            button("🗜 Zip", f"fb:zip1:{page}:{me}"),
            button("✏️ Rename", f"fb:ren:{page}:{me}"),
            button("🗑 Delete", f"fb:delete_confirm:{page}:{me}"),
        ]
    )
    if is_video:
        rows.append(
            [
                button("🎬 Convert", f"fb:conv_menu:{page}:{me}"),
                button("📸 Thumbnails", f"fb:thumb_send:{page}:{me}"),
            ]
        )
    rows.append(back_row(f"fb:list:{page}:{tok(parent_of(rel_path))}"))
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def rename_prompt_screen(rel_path: str, page: int, tok: Tok) -> Screen:
    name = Path(rel_path).name
    suffix = Path(name).suffix
    text = f"✏️ <b>Rename</b> {e(display_name(name))}\n\nSend the new name."
    if suffix:
        text += f" The <code>{e(suffix)}</code> ending is kept if you leave it out."
    return text, InlineKeyboardMarkup([[button("✖ Cancel", f"fb:file:{page}:{tok(rel_path)}")]])


def delete_confirm_screen(
    rel_path: str, is_dir: bool, info: dict[str, Any], page: int, tok: Tok
) -> Screen:
    me = tok(rel_path)
    if is_dir:
        what = (
            f"the folder <b>{e(shown_path(rel_path))}</b> and everything in it "
            f"({info['file_count']} files, {human_size(info['total_size'])})"
        )
        cancel = f"fb:more:{page}:{me}"
    else:
        what = f"<b>{e(Path(rel_path).name)}</b> ({human_size(info['size'])})"
        cancel = f"fb:file:{page}:{me}"
    text = f"⚠️ Permanently delete {what}?\n\nThis cannot be undone."
    rows = [
        [button("🗑 Yes, delete", f"fb:delete_yes:{page}:{me}")],
        [button("✖ Cancel", cancel)],
    ]
    return text, InlineKeyboardMarkup(rows)


def select_screen(
    rel_path: str,
    files: list[dict[str, Any]],
    selected: set[str],
    page: int,
    tok: Tok,
) -> Screen:
    page = clamp_page(page, len(files))
    shown = files[page * PER_PAGE : (page + 1) * PER_PAGE]
    chosen = [f for f in files if f["rel_path"] in selected]
    size = sum(f.get("size", 0) for f in chosen)
    pages = page_count(len(files))
    lines = [f"☑️ <b>Select files</b> · {e(breadcrumb(rel_path))}"]
    if not files:
        lines += ["", "There are no files directly in this folder."]
    else:
        lines.append(
            f"{len(chosen)} of {len(files)} selected"
            + (f" · {human_size(size)}" if chosen else "")
            + (f" · page {page + 1}/{pages}" if pages > 1 else "")
        )
        lines.append("Tap files to pick them, then choose what to do.")

    names = _distinct(shown)
    rows = [
        [
            button(
                entry_label(
                    f, "✅ " if f["rel_path"] in selected else "⬜ ", tidy=names[f["rel_path"]]
                ),
                f"fb:st:{tok(f['rel_path'])}",
            )
        ]
        for f in shown
    ]
    nav = _nav_row(page, len(files), lambda p: f"fb:sp:{p}")
    if nav:
        rows.append(nav)
    if chosen:
        rows.append(
            [
                button(f"📤 Send {len(chosen)}", "fb:sup"),
                button(f"📦 Zip {len(chosen)}", "fb:szip"),
                button(f"🗑 Delete {len(chosen)}", "fb:sdel"),
            ]
        )
    controls = []
    if files:
        controls += [button("☑️ All", "fb:sall"), button("⬜ None", "fb:snone")]
    controls.append(button("✔ Done", "fb:sdone"))
    rows.append(controls)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def delete_selected_confirm_screen(names: list[str], total_size: int) -> Screen:
    preview = "\n".join(f"• {e(short(name, 60))}" for name in names[:10])
    if len(names) > 10:
        preview += f"\n… and {len(names) - 10} more"
    text = (
        f"⚠️ Permanently delete {len(names)} file(s) ({human_size(total_size)})?\n\n"
        f"{preview}\n\nThis cannot be undone."
    )
    rows = [[button("🗑 Yes, delete them", "fb:sdelyes")], [button("✖ Cancel", "fb:sp:-1")]]
    return text, InlineKeyboardMarkup(rows)


def zip_name_screen(count: int, size: int, default_name: str, cancel_data: str) -> Screen:
    text = (
        f"📦 <b>Zip {count} file(s)</b> ({human_size(size)})\n\n"
        f"Send a name for the archive, or use <code>{e(default_name)}</code>."
    )
    rows = [
        [button(f"Use “{short(default_name, 40)}”", "zipname:default")],
        [button("✖ Cancel", cancel_data)],
    ]
    return text, InlineKeyboardMarkup(rows)


def archive_menu_screen(file_count: int, total_size: int, settings_summary: str) -> Screen:
    text = (
        "📦 <b>Archive</b>\n\n"
        "Pack downloaded files into a ZIP or 7Z archive and send it to this chat.\n\n"
        f"Download folder: {file_count} files · {human_size(total_size)}\n"
        f"Settings: {e(settings_summary)}"
    )
    rows = [
        [button("📦 Zip everything", "zip_menu:zip_all")],
        [button("☑️ Choose files…", "fb:sel:0:")],
        [button("⚙️ Archive settings", "zip_menu:settings")],
        back_row(),
    ]
    return text, InlineKeyboardMarkup(rows)
