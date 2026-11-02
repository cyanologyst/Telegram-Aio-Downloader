"""GIF previews, preview videos and the per-chat preview send chain."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

from telegram import InputMediaVideo
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application

from app.bot import telegram_bot as tb
from app.bot.icons import ICON_FAIL
from app.services import animation, gif_previews, upload_filter


def gif_candidates(job: dict) -> list[Path]:
    files: list[Path] = []
    for rel in tb.job_outputs(job):
        full = tb.DOWNLOAD_DIR / rel
        if full.is_dir():
            files += sorted(p for p in full.rglob("*") if p.is_file())
        elif full.is_file():
            files.append(full)
    return [f for f in files if f.suffix.lower() in animation.VIDEO_SUFFIXES]


def prepare_gifs(job: dict, workdir: Path) -> tuple[list[tuple[Path, object]], int, bool]:
    """Pick the job's GIF-like outputs and make them Telegram-ready.

    Returns (files with their media info, how many were too big, whether a clip was cut).
    A GIF job (``job["gif"]``) treats every video as a GIF.
    """
    forced = bool(job.get("gif"))
    ready: list[tuple[Path, object]] = []
    too_big = 0
    trimmed = False
    for path in tb.gif_candidates(job):
        if len(ready) >= tb.GIFS_PER_JOB:
            break
        info = animation.probe(path, tb.FFPROBE_BIN)
        if info is None or not (forced or animation.is_gif_like(path, info)):
            continue
        if animation.ready_as_is(path, info):
            ready.append((path, info))
            continue
        trimmed = trimmed or (info.duration or 0) > animation.GIF_MAX_SECONDS
        target = workdir / f"gif_{len(ready) + too_big}.mp4"
        animation.convert(path, target, ffmpeg=tb.FFMPEG_BIN)
        if target.stat().st_size > animation.BOT_ANIMATION_LIMIT:
            animation.convert(path, target, ffmpeg=tb.FFMPEG_BIN, max_side=480)
        if target.stat().st_size > animation.BOT_ANIMATION_LIMIT:
            too_big += 1
            continue
        ready.append((target, animation.probe(target, tb.FFPROBE_BIN)))
    return ready, too_big, trimmed


async def send_job_gifs(app: Application, job: dict) -> None:
    """Send a finished job's GIFs into the chat as Telegram GIFs (animations)."""
    if job.get("gifs_sent") or job.get("status") != "completed":
        return
    if job.get("provider") not in tb.GIF_PROVIDERS:
        return
    if not job.get("gif") and not tb.get_user_settings(job.get("user_id") or 0).get(
        "send_gifs_to_chat", True
    ):
        return
    job["gifs_sent"] = True
    sent = 0
    with tempfile.TemporaryDirectory(prefix="gifs-") as tmp:
        try:
            gifs, too_big, trimmed = await asyncio.to_thread(tb.prepare_gifs, job, Path(tmp))
        except Exception as exc:
            tb.logger.warning("GIF conversion failed for job #%s: %s", job.get("id"), exc)
            job["gif_note"] = "Couldn't turn it into a GIF; the file is on the server."
            return
        for path, info in gifs:
            try:
                with open(path, "rb") as fh:  # noqa: ASYNC230
                    await app.bot.send_animation(
                        chat_id=job["chat_id"],
                        animation=fh,
                        width=getattr(info, "width", None) or None,
                        height=getattr(info, "height", None) or None,
                        duration=int(getattr(info, "duration", 0) or 0) or None,
                        caption=tb.shorten(tb.clean_download_name(job.get("name") or ""), 200)
                        or None,
                        read_timeout=300,
                        write_timeout=300,
                    )
                sent += 1
            except Exception as exc:
                tb.logger.warning("Could not send GIF for job #%s: %s", job.get("id"), exc)
    notes = []
    if sent:
        notes.append(f"🎞 Sent {'as a GIF' if sent == 1 else f'{sent} GIFs'} above")
    if trimmed:
        notes.append(f"first {animation.GIF_MAX_SECONDS} s only")
    if too_big:
        notes.append(f"{too_big} too big for a GIF")
    if job.get("gif") and not sent and not too_big:
        notes.append("Couldn't make a GIF from this")
    if notes:
        job["gif_note"] = " · ".join(notes)


def job_preview_video(job: dict) -> Path | None:
    """The first real video a finished job produced (GIF jobs have none worth previewing)."""
    if job.get("gif"):
        return None
    return next((p for p in tb.gif_candidates(job) if p.suffix.lower() != ".gif"), None)


def make_gif_previews(source: Path, workdir: Path) -> list[tuple[Path, float, object]]:
    """Cut the preview clips of ``source`` into ``workdir``: [(mp4, start seconds, info)]."""
    if not shutil.which(tb.FFMPEG_BIN) or not shutil.which(tb.FFPROBE_BIN):
        tb.logger.error(
            "GIF previews need ffmpeg and ffprobe (FFMPEG_BIN=%s, FFPROBE_BIN=%s)",
            tb.FFMPEG_BIN,
            tb.FFPROBE_BIN,
        )
        raise RuntimeError("ffmpeg/ffprobe are not installed on the server.")
    info = animation.probe(source, tb.FFPROBE_BIN)
    plan = gif_previews.preview_plan(info.duration if info else None)
    if not plan:
        raise RuntimeError("Couldn't read the video's length.")
    clips = []
    for i, (start, length) in enumerate(plan, start=1):
        target = workdir / f"preview_{i:02d}.mp4"
        try:
            gif_previews.make_clip(source, start, length, target, ffmpeg=tb.FFMPEG_BIN)
        except Exception as exc:
            tb.logger.warning("GIF preview %d of %s failed: %s", i, source.name, exc)
            continue
        if target.stat().st_size <= animation.BOT_ANIMATION_LIMIT:
            clips.append((target, start, animation.probe(target, tb.FFPROBE_BIN)))
    return clips


def preview_video_kwargs(info) -> dict:
    """Size and length of a preview clip, so Telegram lays it out right."""
    return {
        "width": getattr(info, "width", None) or None,
        "height": getattr(info, "height", None) or None,
        "duration": int(getattr(info, "duration", 0) or 0) or None,
        "supports_streaming": True,
    }


async def send_preview_video(app: Application, chat_id: int, path: Path, info) -> None:
    """Send one preview clip, never with a caption."""
    with open(path, "rb") as fh:  # noqa: ASYNC230
        await app.bot.send_video(
            chat_id=chat_id,
            video=fh,
            **tb.preview_video_kwargs(info),
            read_timeout=300,
            write_timeout=300,
        )


async def _send_preview_album(app: Application, chat_id: int, clips: list, name: str) -> int | None:
    """Send the clips (no captions) to one chat; how many went out, or None if that is unknown (timed out)."""
    sent = 0
    try:
        if len(clips) == 1:  # an album needs at least two items
            path, _, info = clips[0]
            await tb.send_preview_video(app, chat_id, path, info)
        else:
            media = []
            for path, _, info in clips:
                with open(  # noqa: ASYNC230
                    path, "rb"
                ) as fh:  # InputMediaVideo reads bytes right away
                    media.append(
                        InputMediaVideo(
                            media=fh,
                            **tb.preview_video_kwargs(info),
                        )
                    )
            await app.bot.send_media_group(
                chat_id=chat_id,
                media=media,
                read_timeout=300,
                write_timeout=300,
            )
        return len(clips)
    except BadRequest as exc:
        tb.logger.warning("GIF preview album of %s refused, sending one by one: %s", name, exc)
        for i, (path, _start, info) in enumerate(clips, start=1):
            try:
                await tb.send_preview_video(app, chat_id, path, info)
                sent += 1
            except Exception as exc:
                tb.logger.warning("Could not send GIF preview %d of %s: %s", i, name, exc)
    except (TimedOut, NetworkError) as exc:
        # BadRequest is a NetworkError too, so this comes after it. The upload
        # may still have landed; resending could duplicate the album.
        tb.logger.warning("GIF preview album of %s did not go through: %s", name, exc)
        return None
    except Exception as exc:
        tb.logger.warning("GIF preview album of %s failed: %s", name, exc)
    return sent


def merge_gif_previews(clips: list, source: Path, workdir: Path) -> tuple[Path, object] | None:
    """Join the clips, in timeline order, into one preview video: (mp4, info), or None if that fails."""
    ordered = [path for path, _, _ in sorted(clips, key=lambda clip: clip[1])]
    target = workdir / gif_previews.merged_name(source.name)
    try:
        gif_previews.merge_clips(ordered, target, ffmpeg=tb.FFMPEG_BIN)
    except Exception as exc:
        tb.logger.warning(
            "Merging the GIF previews of %s failed, sending an album: %s", source.name, exc
        )
        return None
    if target.stat().st_size > tb.BOT_MAX_DOCUMENT_BYTES:
        tb.logger.warning("Merged GIF preview of %s is too big, sending an album", source.name)
        target.unlink(missing_ok=True)
        return None
    return target, animation.probe(target, tb.FFPROBE_BIN)


async def _send_merged_preview(
    app: Application, chat_id: int | str, path: Path, info, *, via_user: bool
) -> bool | None:
    """Send the merged preview (no caption) to one chat; None if that is unknown (timed out)."""
    try:
        if via_user:
            client = await tb.get_pyrogram_client()
            await tb.send_with_flood_wait_handling(
                lambda: client.send_video(
                    chat_id=chat_id,
                    video=str(path),
                    file_name=path.name,
                    supports_streaming=True,
                    **tb.video_send_kwargs(
                        {
                            "width": getattr(info, "width", None),
                            "height": getattr(info, "height", None),
                            "duration": getattr(info, "duration", None),
                        }
                    ),
                )
            )
        else:
            await tb.send_preview_video(app, chat_id, path, info)
        return True
    except BadRequest as exc:
        tb.logger.warning("Merged GIF preview %s refused in %s: %s", path.name, chat_id, exc)
        return False
    except (TimedOut, NetworkError) as exc:
        # BadRequest is a NetworkError too, so this comes after it. The upload
        # may still have landed; resending could duplicate it.
        tb.logger.warning("Merged GIF preview %s did not go through: %s", path.name, exc)
        return None
    except Exception as exc:
        tb.logger.warning(
            "Merged GIF preview %s could not be sent to %s: %s", path.name, chat_id, exc
        )
        return False


async def _preview_sender(target: int | str | None, chat_id: int) -> tuple[int | str, bool]:
    """Where the merged preview goes and whether the user account sends it.

    With an upload ``target`` it lands where the uploaded video went: through
    the Pyrogram account (Saved Messages or the channel/group), or, in
    bot-only mode, the way ``_send_via_bot_api`` sent the video.
    """
    if target is None:
        return chat_id, False
    try:
        await tb.get_pyrogram_client()
    except tb.PyrogramUnavailable:
        return (chat_id if target == "me" else target), False
    return target, True


async def send_gif_previews(
    app: Application,
    chat_id: int,
    source: Path,
    *,
    status_chat_id: int | None = None,
    fallback_chat_id: int | None = None,
    target: int | str | None = None,
) -> int:
    """Send the previews of a video as ONE merged video (up to 10 clips in timeline order).

    Previews never have captions: neither the merged video
    ``Preview - <name>.mp4`` nor the album clips. With an
    upload ``target`` (auto-upload) the video goes to the same chat as the
    uploaded video; otherwise to ``chat_id``. If merging fails, the clips go
    to ``chat_id`` as one album instead (one by one if Telegram refuses the
    album). The "making previews" note goes to ``status_chat_id`` (default
    ``chat_id``); if nothing could be sent, the previews go to
    ``fallback_chat_id``. Returns how many clips were sent.
    """
    try:
        status = await app.bot.send_message(
            chat_id=status_chat_id or chat_id,
            text=f"🎞 Making GIF previews of {tb.shorten(source.name, 80)}…",
        )
    except RetryAfter:
        # Flood control in the bot chat: the preview can still go where the
        # video went (user account, or a channel), so make it without the note.
        status = None
    sent = 0
    with tempfile.TemporaryDirectory(prefix="gif-previews-") as tmp:
        try:
            clips = await asyncio.to_thread(tb.make_gif_previews, source, Path(tmp))
        except Exception as exc:
            tb.logger.warning("GIF previews of %s failed: %s", source.name, exc)
            if status is not None:
                await tb.safe_edit_message(
                    status, f"{ICON_FAIL} No GIF previews: {tb.shorten(str(exc), 200)}"
                )
            return 0
        clips = sorted(clips, key=lambda clip: clip[1])[: gif_previews.PREVIEW_COUNT]
        merged = (
            await asyncio.to_thread(tb.merge_gif_previews, clips, source, Path(tmp))
            if clips
            else None
        )
        if merged:
            path, info = merged
            destinations = [await tb._preview_sender(target, chat_id)]
            if fallback_chat_id is not None and destinations[0] != (fallback_chat_id, False):
                destinations.append((fallback_chat_id, False))
            for destination, via_user in destinations:
                result = await tb._send_merged_preview(
                    app, destination, path, info, via_user=via_user
                )
                if result is None:
                    break  # may have landed; don't send it again anywhere
                if result:
                    sent = len(clips)
                    break
        elif clips:
            destinations = [chat_id]
            if fallback_chat_id is not None and fallback_chat_id != chat_id:
                destinations.append(fallback_chat_id)
            for destination in destinations:
                result = await tb._send_preview_album(app, destination, clips, source.name)
                if result is None:
                    break  # may have landed; don't send it again anywhere
                sent = result
                if sent:
                    break
    try:
        if status is None:
            pass
        elif sent:
            await status.delete()
        else:
            await tb.safe_edit_message(
                status, f"{ICON_FAIL} Couldn't make GIF previews of {tb.shorten(source.name, 80)}."
            )
    except Exception as exc:
        tb.logger.debug("GIF preview status not updated: %s", exc)
    return sent


def _preview_lock() -> asyncio.Lock:
    """One preview job at a time: ffmpeg is heavy and albums should not interleave."""
    loop = asyncio.get_running_loop()
    lock = tb._preview_locks.get(loop)
    if lock is None:
        lock = tb._preview_locks[loop] = asyncio.Lock()
    return lock


def _send_chain(chat_id: int, target: int | str) -> asyncio.Lock:
    """The lock that orders everything uploaded to one destination chat.

    An upload holds it for one file and, for a video, until that video's
    preview is sent too (automatic and manual uploads alike). So two videos finishing together arrive
    as video 1, preview 1, video 2, preview 2, never interleaved.
    """
    key = ("me", chat_id) if target == "me" else target
    chains = tb._send_chains.setdefault(asyncio.get_running_loop(), {})
    lock = chains.get(key)
    if lock is None:
        lock = chains[key] = asyncio.Lock()
    return lock


def wants_upload_previews(user_id: int | None, rel_path: str | Path) -> bool:
    """Whether an upload (automatic or manual) of ``rel_path`` is followed by its GIF preview."""
    return upload_filter.is_video(rel_path) and bool(
        tb.get_user_settings(user_id or 0).get("send_previews_after_upload", True)
    )


async def send_upload_previews(
    app: Application,
    chat_id: int,
    target: int | str,
    rel_path: str,
    *,
    delete_after: bool,
    prune: bool = True,
) -> None:
    """After uploading a video (automatically or by a button), send its merged GIF preview.

    Awaited by the upload, inside its ``_send_chain``, so the preview lands
    right after its own video. It goes to the same chat as the uploaded video
    (``target``: Saved Messages or a channel/group), falling back to this chat
    if it can't be posted there. If merging fails, the album goes to the
    target channel or group, or else this chat. With ``delete_after`` the
    video is deleted once the preview is made (``prune``: and folders it
    left empty).
    """
    preview_chat = target if isinstance(target, int) else chat_id
    try:
        async with tb._preview_lock():
            await tb.send_gif_previews(
                app,
                preview_chat,
                tb.DOWNLOAD_DIR / rel_path,
                status_chat_id=chat_id,
                fallback_chat_id=chat_id,
                target=target,
            )
    except Exception as exc:
        tb.logger.warning("GIF previews after uploading %s failed: %s", rel_path, exc)
    finally:
        if delete_after:
            await asyncio.to_thread(tb.delete_uploaded_file, rel_path, prune=prune)
