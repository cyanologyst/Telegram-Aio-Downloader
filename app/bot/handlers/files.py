"""The file browser: listing, selection, organize, rename and delete."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application

from app.bot import telegram_bot as tb
from app.bot.icons import ICON_BACK, ICON_BROOM, ICON_DELETE, ICON_OK, ICON_PIN, ICON_UPLOAD
from app.bot.views import files as file_views
from app.services import animation
from app.services.organize import OrganizePlan


def is_internal(rel_path: str) -> bool:
    parts = [p for p in Path(rel_path).parts if p not in ("", ".")]
    return bool(parts) and (
        parts[0] in tb.INTERNAL_ROOT_NAMES or any(p.startswith(".") for p in parts)
    )


def list_dir(rel_path: str):
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    if not full.is_dir():
        raise NotADirectoryError(str(full))

    items = []
    for entry in full.iterdir():
        entry_rel = os.path.join(rel_path, entry.name) if rel_path else entry.name
        if tb.is_internal(entry_rel):
            continue
        try:
            st = entry.stat()
            is_dir = entry.is_dir()
            count = 0
            if is_dir:
                try:
                    count = sum(1 for e in os.scandir(entry) if not e.name.startswith("."))
                except OSError:
                    count = 0
            items.append(
                {
                    "name": entry.name,
                    "rel_path": os.path.join(rel_path, entry.name) if rel_path else entry.name,
                    "is_dir": is_dir,
                    "size": st.st_size if entry.is_file() else 0,
                    "mtime": st.st_mtime,
                    "count": count,
                }
            )
        except FileNotFoundError:
            continue

    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return items


def file_info(rel_path: str):
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    if not full.exists():
        raise FileNotFoundError(str(full))
    st = full.stat()
    return {
        "name": full.name,
        "rel_path": rel_path,
        "is_dir": full.is_dir(),
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


def folder_info(rel_path: str):
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    if not full.is_dir():
        raise NotADirectoryError(str(full))

    total_size = 0
    file_count = 0
    folder_count = 0

    for root, dirs, files in os.walk(full):
        folder_count += len(dirs)
        file_count += len(files)
        for f in files:
            fp = Path(root) / f
            with contextlib.suppress(FileNotFoundError):
                total_size += fp.stat().st_size

    st = full.stat()
    return {
        "name": tb.rel_name(rel_path),
        "rel_path": rel_path,
        "folder_count": folder_count,
        "file_count": file_count,
        "total_size": total_size,
        "mtime": st.st_mtime,
    }


def active_job_names() -> set[str]:
    """Names of running jobs. Call on the event loop: download_jobs changes there."""
    names: set[str] = set()
    for job in list(tb.download_jobs.values()):
        if job.get("status") in tb.JOB_ACTIVE_STATES:
            name = str(job.get("name", ""))
            names.update({name, tb.clean_download_name(name)})
    return names


def build_organize_plan(rel_path: str, busy_names: set[str]) -> OrganizePlan:
    folder = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    return tb.plan_organize(
        folder,
        protected_names=tb.ORGANIZE_PROTECTED_NAMES if not rel_path else (),
        busy_names=busy_names,
    )


def build_organize_preview(rel_path: str, page: int, plan: OrganizePlan):
    shown_path = "/" + rel_path.lstrip("/") if rel_path else "/"
    encoded = tb.encode_path(rel_path)
    back = InlineKeyboardButton(f"{ICON_BACK} Back", callback_data=f"fb:list:{page}:{encoded}")
    lines = [f"{ICON_BROOM} Organize videos", f"{ICON_PIN} {shown_path}", ""]
    if not plan.moves:
        lines.append("Nothing to organize: no videos in sub-folders of this folder.")
        if plan.skipped_busy:
            lines.append(f"Skipped {len(plan.skipped_busy)} folder(s) with downloads in progress.")
        return "\n".join(lines), InlineKeyboardMarkup([[back]])

    extra_files = len(plan.moves) - plan.video_count
    lines.append(
        f"Move {plan.video_count} video(s) from {len(plan.folders)} sub-folder(s) into this folder"
        + (f", with {extra_files} matching subtitle file(s)." if extra_files else ".")
    )
    for _, dst in plan.moves[:8]:
        lines.append(f"• {tb.shorten(dst.name, 60)}")
    if len(plan.moves) > 8:
        lines.append(f"… and {len(plan.moves) - 8} more")
    lines.append("")
    if plan.leftover_files:
        lines.append(
            f"Other files left behind: {plan.leftover_files} ({tb.human_size(plan.leftover_bytes)}). "
            "They are kept unless you choose to delete them."
        )
    lines.append("Folders that end up empty are removed.")
    if plan.skipped_busy:
        lines.append(f"Skipped {len(plan.skipped_busy)} folder(s) with downloads in progress.")

    rows = [
        [
            InlineKeyboardButton(
                f"{ICON_OK} Move {plan.video_count} video(s)",
                callback_data=f"fb:organize_go:{page}:{encoded}:keep",
            )
        ]
    ]
    if plan.leftover_files:
        rows.append(
            [
                InlineKeyboardButton(
                    f"{ICON_DELETE} Move + delete leftovers ({tb.human_size(plan.leftover_bytes)})",
                    callback_data=f"fb:organize_go:{page}:{encoded}:purge",
                )
            ]
        )
    rows.append([back])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _entry_for(rel_path: str) -> dict | None:
    full = tb.DOWNLOAD_DIR / rel_path
    try:
        st = full.stat()
    except OSError:
        return None
    is_dir = full.is_dir()
    count = 0
    if is_dir:
        with contextlib.suppress(OSError):
            count = sum(1 for e in os.scandir(full) if not e.name.startswith("."))
    return {
        "name": full.name,
        "rel_path": rel_path,
        "is_dir": is_dir,
        "size": 0 if is_dir else st.st_size,
        "mtime": st.st_mtime,
        "count": count,
    }


def recent_entries(limit: int = file_views.RECENT_COUNT) -> list[dict]:
    """The latest finished downloads, newest first; recent files fill any gap."""
    picked: list[dict] = []
    seen: set[str] = set()
    finished = sorted(
        (j for j in tb.download_jobs.values() if j.get("status") == "completed"),
        key=lambda j: j.get("finished_at") or 0,
        reverse=True,
    )
    for job in finished:
        for rel in tb.job_outputs(job):
            entry = tb._entry_for(rel)
            if entry and rel not in seen and not tb.is_internal(rel):
                seen.add(rel)
                picked.append(entry)
            if len(picked) >= limit:
                return picked
    newest: list[tuple[float, str]] = []
    for count, rel in enumerate(tb.get_all_files_in_folder("")):
        if count > 5000:
            break
        try:
            newest.append(((tb.DOWNLOAD_DIR / rel).stat().st_mtime, rel))
        except OSError:
            continue
    for _, rel in sorted(newest, reverse=True):
        if len(picked) >= limit:
            break
        if rel in seen or any(rel.startswith(s.rstrip("/") + "/") for s in seen):
            continue
        entry = tb._entry_for(rel)
        if entry:
            seen.add(rel)
            picked.append(entry)
    return picked


def browser_entries(rel_path: str, order: str) -> list[dict]:
    """A folder's visible entries: empty folders hidden, sorted."""
    entries = [e for e in tb.list_dir(rel_path) if not (e["is_dir"] and not e["count"])]
    return file_views.sort_entries(entries, order)


async def show_folder(message, rel_path: str, page: int = 0, *, edit: bool = True):
    order = tb.files_sort.get(message.chat_id, "new")
    entries = await asyncio.to_thread(tb.browser_entries, rel_path, order)
    recent = None
    free = None
    if not rel_path and page == 0:
        recent = await asyncio.to_thread(tb.recent_entries)
        on_first_page = {e["rel_path"] for e in entries[: file_views.PER_PAGE]}
        if all(e["rel_path"] in on_first_page for e in recent):
            recent = None  # everything recent is already right there
        try:
            free = shutil.disk_usage(tb.DOWNLOAD_DIR).free
        except OSError:
            free = None
    await tb.show_screen(
        message,
        file_views.browser_screen(
            rel_path, entries, page, tb.encode_path, recent=recent, free_bytes=free, order=order
        ),
        edit=edit,
    )


async def show_file(
    message, rel_path: str, page: int = 0, *, edit: bool = True, user_id: int | None = None
):
    info = await asyncio.to_thread(tb.file_info, rel_path)
    if info["is_dir"]:
        await tb.show_folder(message, rel_path, 0, edit=edit)
        return
    await tb.show_screen(
        message,
        file_views.file_screen(
            rel_path,
            info["size"],
            info["mtime"],
            page,
            tb.encode_path,
            is_video=tb.is_video_file(info["name"]),
            can_send_here=info["size"] <= tb.BOT_MAX_DOCUMENT_BYTES,
            has_saved_messages=tb.pyrogram_unavailable_reason() is None,
            send_limit=tb.BOT_MAX_DOCUMENT_BYTES,
            upload_label=tb.upload_target_label(tb.get_user_settings(user_id or 0)),
        ),
        edit=edit,
    )


def find_files(query: str, limit: int = tb.FIND_LIMIT) -> list[dict]:
    """Files and folders anywhere in Download/ whose name contains ``query``."""
    needle = query.casefold().strip()
    matches: list[dict] = []
    for root, dirs, files in os.walk(tb.DOWNLOAD_DIR):
        rel_root = os.path.relpath(root, tb.DOWNLOAD_DIR)
        rel_root = "" if rel_root == "." else rel_root
        dirs[:] = [d for d in dirs if not tb.is_internal(os.path.join(rel_root, d))]
        for name in dirs + files:
            rel = os.path.join(rel_root, name) if rel_root else name
            if tb.is_internal(rel):
                continue
            if needle in name.casefold() or needle in file_views.display_name(name).casefold():
                entry = tb._entry_for(rel)
                if entry:
                    matches.append(entry)
    matches.sort(key=lambda e: -float(e.get("mtime") or 0))
    return matches[:limit]


def rename_file(rel_path: str, new_name: str) -> str:
    """Rename a file in place; returns the new relative path."""
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    if not full.is_file():
        raise FileNotFoundError(rel_path)
    name = " ".join(new_name.split())
    if not name or name in (".", "..") or name.startswith(".") or re.search(r"[/\\\x00]", name):
        raise ValueError("Use a plain name: no slashes, and not starting with a dot.")
    if len(name.encode()) > 240:
        raise ValueError("That name is too long.")
    if not Path(name).suffix and full.suffix:
        name += full.suffix
    target = full.with_name(name)
    if target.exists():
        raise ValueError(f"There is already a file called “{name}” here.")
    full.rename(target)
    return str(target.relative_to(tb.DOWNLOAD_DIR))


async def send_file_here(app: Application, chat_id: int, rel_path: str) -> None:
    """Send a file of up to 50 MB into the chat, as a video, song, photo or GIF when it is one."""
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    size = full.stat().st_size
    if size > tb.BOT_MAX_DOCUMENT_BYTES:
        raise RuntimeError(f"{full.name} is {tb.human_size(size)}; the chat limit is 50 MB.")
    ext = full.suffix.lower()
    caption = tb.shorten(file_views.display_name(full.name), 200)
    kwargs = {"chat_id": chat_id, "caption": caption, "read_timeout": 300, "write_timeout": 300}
    info = (
        await asyncio.to_thread(animation.probe, full, tb.FFPROBE_BIN)
        if ext in animation.VIDEO_SUFFIXES
        else None
    )
    with open(full, "rb") as fh:  # noqa: ASYNC230
        if ext == ".gif" or (info and animation.is_gif_like(full, info)):
            await app.bot.send_animation(animation=fh, **kwargs)
        elif ext in file_views.VIDEO_EXTS and ext in (".mp4", ".mov", ".m4v"):
            await app.bot.send_video(
                video=fh,
                supports_streaming=True,
                width=getattr(info, "width", None) or None,
                height=getattr(info, "height", None) or None,
                duration=int(getattr(info, "duration", 0) or 0) or None,
                **kwargs,
            )
        elif ext in (".mp3", ".m4a"):
            await app.bot.send_audio(audio=fh, **kwargs)
        elif ext in (".jpg", ".jpeg", ".png", ".webp") and size <= 10 * 1024 * 1024:
            await app.bot.send_photo(photo=fh, **kwargs)
        else:
            await app.bot.send_document(document=fh, **kwargs)


def files_wait(context, kind: str) -> dict | None:
    """The pending rename/find prompt of this kind, if it hasn't expired."""
    wait = context.user_data.get("files_wait")
    if not wait or wait.get("kind") != kind:
        return None
    if time.time() - wait.get("since", 0) > tb.FILES_WAIT_SECONDS:
        context.user_data.pop("files_wait", None)
        return None
    return wait


def files_in_folder(rel_path: str) -> list[dict]:
    return [entry for entry in tb.list_dir(rel_path) if not entry["is_dir"]]


async def show_selection(message, user_id: int):
    session = tb.batch_select_sessions[user_id]
    files = await asyncio.to_thread(tb.files_in_folder, session["rel_path"])
    session["selected"] &= {f["rel_path"] for f in files}  # forget deleted files
    await tb.show_screen(
        message,
        file_views.select_screen(
            session["rel_path"], files, session["selected"], session["page"], tb.encode_path
        ),
    )


async def handle_file_selection(update: Update, context, action: str, parts: list[str]):
    """Selection mode in the file browser: pick files, then upload, zip or delete them."""
    query = update.callback_query
    user_id = query.from_user.id
    session = tb.batch_select_sessions.get(user_id)
    if session is None:
        await tb.answer_once(
            query, "This selection has expired. Open Files again.", show_alert=True
        )
        return
    if action == "bupload_empty":
        await tb.answer_once(query, "Select at least one file first.", show_alert=True)
        return

    if action == "st":
        token = parts[2] if len(parts) > 2 else ""
        session["selected"].symmetric_difference_update({tb.decode_path(token)})
    elif action == "sp":
        page_raw = parts[2] if len(parts) > 2 else "0"
        if page_raw != "-1":
            session["page"] = int(page_raw)
    elif action in ("sall", "snone"):
        files = await asyncio.to_thread(tb.files_in_folder, session["rel_path"])
        session["selected"] = {f["rel_path"] for f in files} if action == "sall" else set()
    elif action == "sdone":
        tb.batch_select_sessions.pop(user_id, None)
        await tb.show_folder(query.message, session["rel_path"], session["page"])
        return
    else:
        selected = sorted(session["selected"])
        if not selected:
            await tb.answer_once(query, "Select at least one file first.", show_alert=True)
            return
        if action == "sup":
            tb.batch_select_sessions.pop(user_id, None)
            await tb.answer_upload_tap(query)
            await tb.safe_edit_message(
                query.message, f"{ICON_UPLOAD} Preparing upload of {len(selected)} file(s)..."
            )
            tb.run_in_background(
                tb.send_folder_files_via_pyrogram(
                    context.application,
                    query.message.chat_id,
                    query.message.message_id,
                    session["rel_path"],
                    file_list=selected,
                    user_id=user_id,
                ),
                name="upload",
                on_error=tb.message_error_reporter(
                    context.application, query.message.chat_id, query.message.message_id, "Upload"
                ),
            )
            return
        if action == "szip":
            paths = tb.filter_files_for_archiving(
                [tb.safe_join(tb.DOWNLOAD_DIR, rel) for rel in selected]
            )
            if not paths:
                await tb.answer_once(
                    query, "None of the selected files can be zipped.", show_alert=True
                )
                return
            await tb.ask_zip_name(
                query.message,
                user_id,
                query.message.chat_id,
                paths,
                default_name=(
                    tb.rel_name(session["rel_path"])
                    if session["rel_path"]
                    else tb.default_archive_name()
                ),
                cancel_data="fb:sp:-1",
            )
            return
        if action == "sdel":
            infos = [await asyncio.to_thread(tb.file_info, rel) for rel in selected]
            await tb.show_screen(
                query.message,
                file_views.delete_selected_confirm_screen(
                    [info["name"] for info in infos], sum(info["size"] for info in infos)
                ),
            )
            return
        if action == "sdelyes":
            deleted, errors = await asyncio.to_thread(tb.delete_paths_batch, selected)
            session["selected"] = set()
            await tb.answer_once(
                query,
                f"Deleted {deleted} file(s)."
                + (f" {len(errors)} could not be deleted." if errors else ""),
                show_alert=bool(errors),
            )
    await tb.show_selection(query.message, user_id)


def is_manga_gallery_folder(folder: Path) -> bool:
    try:
        folder.relative_to(tb.MANGA_DIR)
    except ValueError:
        return False
    return folder.is_dir() and bool(tb.list_manga_images(folder))


def delete_path(rel_path: str):
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    if not full.exists():
        raise FileNotFoundError(str(full))
    if full.is_dir():
        shutil.rmtree(full)
        return "folder"
    full.unlink()
    return "file"


def delete_all_in_directory(rel_path: str) -> tuple:
    """Delete every file and folder directly inside rel_path."""
    files_deleted = 0
    folders_deleted = 0
    errors = []
    for item in tb.list_dir(rel_path):
        try:
            kind = tb.delete_path(item["rel_path"])
            if kind == "file":
                files_deleted += 1
            else:
                folders_deleted += 1
        except Exception as e:
            errors.append(f"{item['name']}: {e}")
    return files_deleted, folders_deleted, errors


def delete_paths_batch(rel_paths: list) -> tuple:
    """Delete multiple paths. Returns (deleted_count, errors)."""
    deleted = 0
    errors = []
    for rel in rel_paths:
        try:
            tb.delete_path(rel)
            deleted += 1
        except Exception as e:
            errors.append(f"{rel}: {e}")
    return deleted, errors


def is_duplicate_name(name: str):
    return any(name in files for _, _, files in os.walk(tb.DOWNLOAD_DIR))
