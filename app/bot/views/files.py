"""File browser screens.

Entries are dicts with ``name``, ``rel_path``, ``is_dir``, ``size`` and, for
folders, ``count`` (items inside). ``tok`` turns a relative path into the
short token used in callback data (Telegram caps callback data at 64 bytes).

Callback data (``fb:<action>:<page>:<token>``, page = the folder page to
return to):
  list  open a folder page         o     open an entry (folder or file)
  file  file details               more  folder actions
  send_yes / send_folder_confirm   upload a file / a whole folder
  delete_confirm, deleteall_confirm, organize, manga_pdf, conv_menu, thumb_send
Selection mode (session kept per user): sel, st:<token>, sp:<page>, sall,
snone, sup, szip, sdel, sdelyes, sdone.
"""

from __future__ import annotations

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
    rows_of,
    short,
)

PER_PAGE = 8
PER_ROW = 4

Tok = Callable[[str], str]

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".3gp"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}


def icon(name: str, is_dir: bool) -> str:
    if is_dir:
        return "📁"
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXTS:
        return "🎞"
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


def shown_path(rel_path: str) -> str:
    return "/" + rel_path.strip("/") if rel_path else "/"


def parent_of(rel_path: str) -> str:
    parent = str(Path(rel_path).parent) if rel_path else ""
    return "" if parent == "." else parent


def page_count(total: int) -> int:
    return max(1, (total + PER_PAGE - 1) // PER_PAGE)


def clamp_page(page: int, total: int) -> int:
    return max(0, min(page, page_count(total) - 1))


def _entry_line(number: int, entry: dict[str, Any], selected: bool | None = None) -> str:
    mark = "" if selected is None else ("✅ " if selected else "⬜ ")
    if entry["is_dir"]:
        detail = f"{entry.get('count', 0)} items"
    else:
        detail = human_size(entry.get("size", 0))
    name = e(short(entry["name"], 48))
    return f"{number}. {mark}{icon(entry['name'], entry['is_dir'])} {name} — {detail}"


def _nav_row(page: int, total: int, data_for_page: Callable[[int], str]) -> list[Any] | None:
    pages = page_count(total)
    if pages <= 1:
        return None
    row = []
    if page > 0:
        row.append(button("◀", data_for_page(page - 1)))
    row.append(button(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        row.append(button("▶", data_for_page(page + 1)))
    return row


def browser_screen(rel_path: str, entries: list[dict[str, Any]], page: int, tok: Tok) -> Screen:
    page = clamp_page(page, len(entries))
    here = tok(rel_path)
    shown = entries[page * PER_PAGE : (page + 1) * PER_PAGE]
    lines = [f"📁 <b>{e(shown_path(rel_path))}</b>"]
    if not entries:
        lines += ["", "This folder is empty."]
    else:
        pages = page_count(len(entries))
        lines.append(f"{len(entries)} items" + (f" · page {page + 1}/{pages}" if pages > 1 else ""))
        lines.append("")
        lines += [_entry_line(page * PER_PAGE + i + 1, entry) for i, entry in enumerate(shown)]
        lines += ["", "Tap a number to open it."]

    numbers = [
        button(str(page * PER_PAGE + i + 1), f"fb:o:{page}:{tok(entry['rel_path'])}")
        for i, entry in enumerate(shown)
    ]
    rows = rows_of(numbers, PER_ROW)
    nav = _nav_row(page, len(entries), lambda p: f"fb:list:{p}:{here}")
    if nav:
        rows.append(nav)
    if entries:
        rows.append(
            [
                button("☑️ Select", f"fb:sel:{page}:{here}"),
                button("⋯ More", f"fb:more:{page}:{here}"),
            ]
        )
    bottom = [button("⬆ Up", f"fb:list:0:{tok(parent_of(rel_path))}")] if rel_path else []
    bottom += [button("🔄", f"fb:list:{page}:{here}"), home_button()]
    rows.append(bottom)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


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
    rel_path: str, size: int, mtime: float, page: int, tok: Tok, *, is_video: bool
) -> Screen:
    me = tok(rel_path)
    name = Path(rel_path).name
    modified = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    text = (
        f"{icon(name, False)} <b>{e(name)}</b>\n\n"
        f"{human_size(size)} · modified {modified}\n"
        f"<code>{e(shown_path(rel_path))}</code>"
    )
    rows = [[button("📤 Upload to Saved Messages", f"fb:send_yes:{page}:{me}")]]
    if is_video:
        rows.append(
            [
                button("🎬 Convert", f"fb:conv_menu:{page}:{me}"),
                button("📸 Thumbnails", f"fb:thumb_send:{page}:{me}"),
            ]
        )
    rows.append([button("🗑 Delete", f"fb:delete_confirm:{page}:{me}")])
    rows.append(back_row(f"fb:list:{page}:{tok(parent_of(rel_path))}", "⬅ Folder"))
    return text, InlineKeyboardMarkup(rows)


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
    lines = [f"☑️ <b>Select files</b> · {e(shown_path(rel_path))}"]
    if not files:
        lines += ["", "There are no files directly in this folder."]
    else:
        lines.append(
            f"{len(chosen)} of {len(files)} selected" + (f" · {human_size(size)}" if chosen else "")
        )
        lines.append("")
        lines += [
            _entry_line(page * PER_PAGE + i + 1, f, f["rel_path"] in selected)
            for i, f in enumerate(shown)
        ]
        lines += ["", "Tap numbers to select, then choose what to do."]

    numbers = [
        button(
            ("✅" if f["rel_path"] in selected else "") + str(page * PER_PAGE + i + 1),
            f"fb:st:{tok(f['rel_path'])}",
        )
        for i, f in enumerate(shown)
    ]
    rows = rows_of(numbers, PER_ROW)
    nav = _nav_row(page, len(files), lambda p: f"fb:sp:{p}")
    if nav:
        rows.append(nav)
    if files:
        rows.append([button("☑️ All", "fb:sall"), button("⬜ None", "fb:snone")])
    if chosen:
        rows.append(
            [
                button(f"📤 Upload {len(chosen)}", "fb:sup"),
                button(f"📦 Zip {len(chosen)}", "fb:szip"),
                button(f"🗑 Delete {len(chosen)}", "fb:sdel"),
            ]
        )
    rows.append([button("✖ Done", "fb:sdone")])
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
