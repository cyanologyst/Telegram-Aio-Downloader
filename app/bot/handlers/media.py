"""Media file helpers: type detection, captions, chunking, metadata, thumbnails."""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from app.bot import telegram_bot as tb
from app.bot.icons import ICON_FAIL, ICON_WARN
from app.services import upload_guard, video_split


def is_video_file(file_path: str) -> bool:
    """Check if file is a video."""
    video_exts = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".3gp", ".m3u8"}
    return Path(file_path).suffix.lower() in video_exts


def is_audio_file(file_path: str) -> bool:
    """Check if file is audio."""
    audio_exts = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma", ".opus", ".aiff"}
    return Path(file_path).suffix.lower() in audio_exts


def is_image_file(file_path: str) -> bool:
    """Check if file is an image."""
    image_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".svg"}
    return Path(file_path).suffix.lower() in image_exts


def get_send_method(file_path: str) -> str:
    """Determine the best send method: document, video, audio, or photo."""
    if tb.is_video_file(file_path):
        return "video"
    if tb.is_audio_file(file_path):
        return "audio"
    if tb.is_image_file(file_path):
        return "photo"
    return "document"


def build_upload_caption(rel_path: str) -> str:
    """Build a formatted caption with file metadata."""
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    if not full.exists():
        return f"Uploaded: {rel_path}"

    st = full.stat()
    size = tb.human_size(st.st_size)
    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    name = full.name
    path = "/" + rel_path.lstrip("/")

    return f"📄 {name}\n\n" f"📊 Size: {size}\n" f"📅 Modified: {mtime}\n" f"📍 Path: {path}"


def build_progress_bar(current: int, total: int, width: int = 20) -> str:
    """Build a visual progress bar."""
    if total <= 0:
        pct = 0
        filled = 0
    else:
        ratio = max(0.0, min(1.0, current / total))
        pct = int(ratio * 100)
        filled = int(ratio * width)

    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {pct}%"


def split_file_into_chunks(file_path: str, chunk_size: int = video_split.DEFAULT_PART_CAP) -> list:
    """
    Split a file into chunks if it exceeds chunk_size.
    Returns a list of (chunk_path, chunk_index, total_chunks) tuples.
    """
    full = tb.safe_join(tb.DOWNLOAD_DIR, file_path)
    file_size = full.stat().st_size

    if file_size <= chunk_size:
        return [(str(full), 1, 1)]

    chunk_dir = full.parent / f"{full.stem}_chunks"
    chunk_dir.mkdir(exist_ok=True)

    chunks = []
    chunk_num = 1
    total_chunks = math.ceil(file_size / chunk_size)

    copy_buffer_size = 8 * 1024 * 1024
    with open(full, "rb") as src:
        while chunk_num <= total_chunks:
            chunk_path = chunk_dir / f"{full.stem}_part_{chunk_num:03d}{full.suffix}"
            with open(chunk_path, "wb") as dst:
                remaining = min(chunk_size, file_size - src.tell())
                while remaining > 0:
                    block = src.read(min(copy_buffer_size, remaining))
                    if not block:
                        break
                    dst.write(block)
                    remaining -= len(block)

            chunks.append((str(chunk_path), chunk_num, total_chunks))
            chunk_num += 1

    return chunks


def cleanup_file_chunks(chunks: list) -> None:
    """Delete temporary split-upload parts and the folder holding them.

    split_file_into_chunks() copies the whole file into <stem>_chunks/ when it
    exceeds the Telegram part limit, so skipping this leaves a full duplicate
    on disk for every large upload.
    """
    chunk_dirs: set[Path] = set()
    for chunk_path, _, chunk_total in chunks:
        if chunk_total <= 1:
            continue
        chunk = Path(chunk_path)
        chunk_dirs.add(chunk.parent)
        try:
            if chunk.exists():
                chunk.unlink()
        except OSError:
            tb.logger.warning("Could not remove temporary upload chunk: %s", chunk)

    for chunk_dir in chunk_dirs:
        with contextlib.suppress(OSError):
            chunk_dir.rmdir()


def prepare_upload_parts(rel_path: str, cap: int) -> tuple[list, bool]:
    """The pieces to upload for ``rel_path``: (parts, send them as documents).

    Parts are split_file_into_chunks()-style (path, index, total) tuples. A
    video over ``cap`` is cut by ffmpeg into parts that each play; anything
    else (or a video ffmpeg can't cut) is split into raw byte chunks, which
    only make sense as documents to join back together.
    """
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    if full.stat().st_size <= cap:
        return [(str(full), 1, 1)], False
    if tb.get_send_method(str(full)) == "video":
        parts = video_split.split_video(
            full,
            full.parent / f"{full.stem}_chunks",
            cap,
            ffmpeg=tb.FFMPEG_BIN,
            ffprobe=tb.FFPROBE_BIN,
        )
        if parts:
            return [(str(p), i, len(parts)) for i, p in enumerate(parts, start=1)], False
        tb.logger.warning("Couldn't split %s into playable parts; sending byte chunks", rel_path)
    return tb.split_file_into_chunks(rel_path, cap), True


async def upload_part_cap(client) -> int:
    """How big one upload may be for the logged-in account (Premium doubles it)."""
    try:
        me = await client.get_me()
    except Exception as exc:
        tb.logger.debug("get_me failed, assuming no Premium: %s", exc)
        return video_split.DEFAULT_PART_CAP
    return video_split.part_cap(bool(getattr(me, "is_premium", False)))


def _positive_int(value) -> int | None:
    """``value`` as a whole number above zero, or None if it isn't one.

    ffprobe prints ``N/A`` (or nothing) for files it can't fully read, such as
    an MP4 whose moov atom hasn't been downloaded yet.
    """
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number)


def video_send_kwargs(meta) -> dict:
    """width/height/duration for Pyrogram's send_video, leaving out bad values.

    Pyrogram packs these as raw numbers, so a None or a string crashes the
    upload (struct.error); a missing key falls back to Pyrogram's default of 0.
    """
    meta = meta or {}
    kwargs = {}
    for key in ("width", "height", "duration"):
        value = tb._positive_int(meta.get(key))
        if value is not None:
            kwargs[key] = value
    return kwargs


def get_video_metadata(file_path: str):
    """Extract video width, height and duration using ffprobe.

    Each value is a positive int, or None when ffprobe couldn't tell.
    """
    meta = {"width": None, "height": None, "duration": None}
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=0",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return meta

    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key in meta and meta[key] is None:
            meta[key] = tb._positive_int(value)
    return meta


async def unfinished_aria2_downloads() -> list[dict]:
    """aria2's active and waiting downloads, with their files.

    Calls RPC directly instead of tell_active(), which would start aria2 if it
    isn't running; raises if it can't be reached.
    """
    active = await tb.aria2_client.call("aria2.tellActive", tb.UPLOAD_GUARD_STATUS_KEYS)
    waiting = await tb.aria2_client.call("aria2.tellWaiting", 0, 1000, tb.UPLOAD_GUARD_STATUS_KEYS)
    return list(active or []) + list(waiting or [])


async def _video_metadata_readable(path: Path) -> bool:
    meta = await asyncio.to_thread(tb.get_video_metadata, str(path))
    return any(meta.get(key) for key in ("width", "height", "duration"))


async def upload_refusal(path: str | Path) -> str | None:
    """Why this file (relative to Download/, or absolute) must not be uploaded yet, or None."""
    full = Path(path) if Path(path).is_absolute() else tb.safe_join(tb.DOWNLOAD_DIR, str(path))
    return await upload_guard.check_upload(
        full,
        fetch_statuses=tb.unfinished_aria2_downloads,
        probe_readable=tb._video_metadata_readable,
    )


def cleanup_orphan_thumbnails(directory: Path = None) -> None:
    """Remove stale generated thumbnail files."""
    root = directory or tb.DOWNLOAD_DIR
    try:
        for thumb in root.rglob(".thumb_*.jpg"):
            try:
                if thumb.is_file():
                    thumb.unlink()
            except Exception:
                pass
    except Exception:
        pass


def generate_thumbnail(file_path: str, output_size: tuple = (320, 180)) -> str:
    """Generate a contact sheet thumbnail for video using new thumbnail_generate module.
    Returns path to thumbnail or empty string."""
    if not tb.HAS_PIL:
        return ""

    path = Path(file_path)
    if path.is_absolute():
        full = path.resolve()
        base_abs = tb.DOWNLOAD_DIR.resolve()
        if not (full == base_abs or str(full).startswith(str(base_abs) + os.sep)):
            return ""
    else:
        full = tb.safe_join(tb.DOWNLOAD_DIR, file_path)

    try:
        if tb.is_video_file(str(full)):
            # Use new thumbnail_generate module for video contact sheets
            thumb_path = full.parent / f".thumb_{full.stem}.jpg"

            # Generate contact sheet using the new module
            tb.generate_contact_sheet(str(full), str(thumb_path))

            if thumb_path.exists():
                return str(thumb_path)
        elif tb.is_image_file(str(full)):
            # For images, create a simple thumbnail
            img = tb.Image.open(full)
            resample_filter = getattr(
                getattr(tb.Image, "Resampling", tb.Image), "LANCZOS", tb.Image.LANCZOS
            )
            img.thumbnail(output_size, resample_filter)
            thumb_path = full.parent / f".thumb_{full.stem}.jpg"
            img.save(thumb_path, "JPEG", quality=80)
            return str(thumb_path)
    except Exception as e:
        tb.logger.debug(f"Error generating thumbnail for {file_path}: {e}")

    return ""


def parse_ffmpeg_duration(ffmpeg_stderr: str) -> float | None:
    for line in ffmpeg_stderr.splitlines():
        if "Duration:" in line:
            try:
                h, m, sec = line.split("Duration:")[1].split(",")[0].strip().split(":")
                return int(h) * 3600 + int(m) * 60 + float(sec)
            except (ValueError, IndexError):
                return None
    return None


def parse_ffmpeg_progress_seconds(line: str) -> float | None:
    """Seconds encoded so far from an ``-progress`` line (out_time_us/out_time_ms are µs)."""
    key, _, value = line.strip().partition("=")
    if key in ("out_time_us", "out_time_ms"):
        try:
            return int(value) / 1_000_000
        except ValueError:
            return None
    return None


async def convert_video_quality(
    input_path: str, output_path: str, target_res: str, progress_callback=None
):
    """
    Re-encode video to given resolution (e.g. '720p') with libx264, CRF 23.

    Progress comes from ``-progress pipe:1``: ffmpeg's normal stderr status line
    is redrawn with carriage returns, so reading it line by line only returned
    once the encode had finished and the progress bar stayed at 0%.
    """
    resolution_map = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
    target_h = resolution_map.get(target_res, 720)

    probe = await asyncio.to_thread(
        subprocess.run,
        [tb.FFMPEG_BIN, "-i", input_path],
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    duration = tb.parse_ffmpeg_duration(probe.stderr)

    cmd = [
        tb.FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-i",
        input_path,
        "-vf",
        f"scale=-2:{target_h}",
        "-c:v",
        "libx264",
        "-crf",
        "23",
        "-preset",
        "medium",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        "-y",
        output_path,
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stderr_task = asyncio.create_task(process.stderr.read())
    last_update = 0.0
    try:
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            seconds = tb.parse_ffmpeg_progress_seconds(raw.decode("utf-8", errors="ignore"))
            if progress_callback and duration and seconds is not None:
                now = time.time()
                if now - last_update > 3.0:
                    last_update = now
                    await progress_callback(min(99, int(seconds / duration * 100)))
        await process.wait()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        Path(output_path).unlink(missing_ok=True)  # noqa: ASYNC240
        raise
    stderr = (await stderr_task).decode("utf-8", errors="ignore").strip()
    if process.returncode != 0:
        Path(output_path).unlink(missing_ok=True)  # noqa: ASYNC240
        raise RuntimeError(f"ffmpeg conversion failed: {stderr[-300:] or process.returncode}")


async def send_thumbnail(update: Update, context: ContextTypes.DEFAULT_TYPE, rel_path: str):
    """Send a contact sheet (grid of frames from across the video) as a photo."""
    full = tb.safe_join(tb.DOWNLOAD_DIR, rel_path)
    chat_id = update.effective_chat.id

    if not full.exists() or not tb.is_video_file(str(full)):
        await context.bot.send_message(
            chat_id=chat_id, text=f"{ICON_WARN} {full.name} is not a video file."
        )
        return

    tmp_dir = Path(tempfile.gettempdir()) / f"thumb_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    output_path = tmp_dir / "contact_sheet.jpg"
    try:
        # Decoding frames is CPU-heavy; keep it off the event loop.
        await asyncio.to_thread(tb.generate_contact_sheet, str(full), str(output_path))
        if not output_path.exists():
            raise RuntimeError("no image was produced")
        with open(output_path, "rb") as img:  # noqa: ASYNC230
            await context.bot.send_photo(
                chat_id=chat_id, photo=img, caption=f"📸 Thumbnails: {full.name}"
            )
    except Exception as exc:
        tb.logger.warning("Thumbnail generation failed for %s: %s", full, exc)
        await context.bot.send_message(
            chat_id=chat_id, text=f"{ICON_FAIL} Could not make thumbnails for {full.name}: {exc}"
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
