"""yt-dlp/cobalt options and error handling, prompts and download slots."""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application
from yt_dlp.networking.impersonate import ImpersonateTarget

from app.bot import telegram_bot as tb
from app.bot.icons import ICON_AUDIO, ICON_DOWNLOAD, ICON_IMAGE
from app.bot.views import jobs as job_views
from app.services import animation, page_sniffer
from app.services.cobalt import CobaltError
from app.services.hentai_playlist import HentaiPlaylist
from app.services.page_sniffer import Candidate, SniffResult
from app.services.pornhub_model import PornHubModelPlaylist


def cookies_file() -> str | None:
    """The cookies.txt to use: the one sent to the bot, else YTDLP_COOKIES_FILE."""
    if tb.UPLOADED_COOKIES_PATH.is_file():
        return str(tb.UPLOADED_COOKIES_PATH)
    if tb.YTDLP_COOKIES_FILE and Path(tb.YTDLP_COOKIES_FILE).is_file():
        return tb.YTDLP_COOKIES_FILE
    return None


def ytdlp_common_options(url: str, resolved_video) -> dict:
    """yt-dlp options shared by downloads and the quality probe."""
    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if cookies := tb.cookies_file():
        opts["cookiefile"] = cookies
    if tb.YTDLP_JS_RUNTIMES:
        opts["js_runtimes"] = dict(tb.YTDLP_JS_RUNTIMES)
    if tb.YTDLP_PROXY:
        opts["proxy"] = tb.YTDLP_PROXY
    if tb.requires_ytdlp_generic_impersonation(url):
        opts["extractor_args"] = {"generic": {"impersonate": ["chrome"]}}
    if tb.requires_ytdlp_impersonation(url):
        opts["impersonate"] = ImpersonateTarget.from_str("chrome")
    if resolved_video.referer:
        opts["http_headers"]["Referer"] = resolved_video.referer
    return opts


def probe_video(url: str) -> dict:
    """Title, duration and available video heights, without downloading."""
    resolved = tb.resolve_adult_video_url(url)
    opts = tb.ytdlp_common_options(url, resolved)
    opts["skip_download"] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(resolved.url, download=False) or {}
    heights = sorted(
        {
            int(fmt["height"])
            for fmt in info.get("formats") or []
            if fmt.get("height") and fmt.get("vcodec") not in (None, "none")
        }
    )
    if not heights and info.get("height"):
        heights = [int(info["height"])]
    return {"title": info.get("title") or "", "duration": info.get("duration"), "heights": heights}


def explain_ytdlp_error(error) -> tuple[str, bool]:
    """A short, readable reason for a yt-dlp error, and whether retrying is pointless."""
    text = " ".join(str(error).split())
    normalized = text.replace("\u2019", "'").lower()
    for phrase, explanation in tb.YTDLP_KNOWN_ERRORS:
        if phrase in normalized:
            return explanation, True
    text = re.sub(r"^ERROR:\s*", "", text)
    text = re.sub(r"^\[[^\]]+\]\s*[^:\s]+:\s*", "", text)  # "[youtube] abc123: "
    text = re.split(r"\s(?:Use --|See https?://)", text, maxsplit=1)[0]
    return text or "Unknown error", False


class DownloadFailed(RuntimeError):
    """A failure whose readable reason is already worked out."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def download_with_cobalt(
    job: dict, url: str, output_dir: Path, first_reason: str, is_cancelled
):
    """Second try through cobalt after the first engine failed with ``first_reason``.

    Returns (title, filepath or None); multi-item posts land in a folder listed in
    ``job["outputs"]``.
    """
    tb.logger.info("First engine failed for %s (%s); trying cobalt", url, first_reason)
    job.update(engine="cobalt", note="via cobalt", status="downloading", last_line="")
    try:
        items = await tb.cobalt_client.resolve(
            url,
            audio_only=bool(job.get("audio_only")),
            mute=bool(job.get("gif")),
            max_height=job.get("max_height"),
        )
        destination = output_dir
        if len(items) > 1:
            destination = output_dir / f"cobalt_{job['id']}"
        paths: list[Path] = []
        finished_bytes = 0
        for item in items:

            def progress(done: int, total: int, base: int = finished_bytes):
                job["completed_length"] = base + done
                job["total_length"] = base + total if total else 0

            path = await tb.cobalt_client.download(
                item, destination, progress=progress, is_cancelled=is_cancelled
            )
            paths.append(path)
            finished_bytes += path.stat().st_size
    except CobaltError as exc:
        if exc.code == "cancelled":
            raise
        if exc.code in tb.COBALT_UNSUPPORTED:
            raise tb.DownloadFailed(first_reason) from exc
        raise tb.DownloadFailed(f"{first_reason}\n\ncobalt: {exc}") from exc
    job["completed_length"] = job["total_length"] = finished_bytes
    if len(paths) == 1:
        return paths[0].stem, str(paths[0])
    job["outputs"] = [str(destination)]
    return f"{len(paths)} files from {job.get('platform') or 'the post'}", None


def explain_gallery_error(error, category: str | None) -> tuple[str, bool]:
    """(readable reason, whether cookies would help) for a gallery-dl failure."""
    text = " ".join(str(error).split())
    lowered = text.lower()
    site = tb.site_label(category) if category else "The site"
    if any(phrase in lowered for phrase in tb.SITE_BLOCK_PHRASES):
        return (
            f"{site} is blocking this server's IP address. Cookies from a browser logged in to "
            f"{site} usually get through (one cookies.txt can hold several sites). "
            + tb._COOKIE_HINT,
            True,
        )
    if any(word in lowered for word in ("authorizationerror", "login required", "log in")):
        return f"{site} wants you logged in for this. " + tb._COOKIE_HINT, True
    text = re.sub(r"^gallery-dl failed:\s*", "", text)
    text = re.sub(r"\[[\w.-]+\]\[error\]\s*", "", text)
    return text.strip().strip('"') or "gallery-dl failed", False


def explain_spotdl_error(error) -> tuple[str, bool]:
    """(readable reason, whether YouTube cookies would help) for a spotDL failure."""
    text = " ".join(str(error).split())
    lowered = text.lower()
    if any(
        s in lowered for s in ("audioprovidererror", "yt-dlp download error", "no usable results")
    ):
        return (
            "spotDL found the song on Spotify, but it gets the audio from YouTube and YouTube "
            "refused this server. " + tb._COOKIE_HINT,
            True,
        )
    return text[-400:] or "spotDL failed", False


def quality_actions(heights: list[int], duration: float | None = None) -> list[tuple[str, str]]:
    """Picker buttons: Best (with its height when known), lower standard heights, MP3,
    and GIF for clips of up to a minute."""
    top = max(heights) if heights else None
    actions = [(f"⭐ Best ({top}p)" if top else "⭐ Best", "best")]
    for h in tb.QUALITY_STEPS:
        # Offer a cap only when it differs from Best (the video has more than h).
        if top is None and h == 720 or (top is not None and top > h):
            actions.append((f"{h}p", f"h{h}"))
    actions.append(("🎵 MP3", "mp3"))
    if duration is None or duration <= animation.GIF_MAX_SECONDS:
        actions.append(("🎞 GIF", "gif"))
    return actions


async def show_quality_picker(message, rid: str, url: str, platform: str):
    """Fill the video prompt with what the video actually offers."""
    try:
        info = await asyncio.wait_for(asyncio.to_thread(tb.probe_video, url), timeout=60)
    except Exception as exc:
        tb.logger.info("Quality probe failed for %s: %s", url, exc)
        reason, fatal = tb.explain_ytdlp_error(exc)
        if fatal:
            if tb.link_requests.pop(rid, None) is None:
                return  # cancelled while we were looking
            markup = None
            if tb._COOKIE_HINT in reason:
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🍪 Add cookies", callback_data="nav:cookies")]]
                )
            await tb.safe_edit_message(
                message,
                f"❌ <b>Can't download this {html.escape(platform)} link</b>\n\n"
                f"{html.escape(reason)}",
                markup,
                parse_mode=ParseMode.HTML,
            )
            return
        info = {"title": "", "duration": None, "heights": []}
    if rid not in tb.link_requests:
        return  # cancelled while we were looking
    lines = [f"🎬 <b>{html.escape(tb.shorten(info['title'] or platform + ' video', 90))}</b>"]
    meta = platform
    if info.get("duration"):
        meta += f" · {job_views.duration(info['duration'])}"
    lines += [html.escape(meta), "", "Choose a quality:"]
    if not info["heights"]:
        lines.append("<i>(Couldn't list the qualities; Best picks the highest available.)</i>")
    await tb.safe_edit_message(
        message,
        "\n".join(lines),
        tb.link_request_markup(rid, tb.quality_actions(info["heights"], info.get("duration"))),
        parse_mode=ParseMode.HTML,
    )


def video_default_choice(user_id: int) -> tuple[bool, int | None] | None:
    """(audio_only, max_height) from Settings → Video links, or None to ask."""
    value = str(tb.get_user_settings(user_id).get("video_default", "ask"))
    if value == "mp3":
        return True, None
    if value == "best":
        return False, None
    if value.isdigit():
        return False, int(value)
    return None


# Sites whose players can't be read from a page (scrambled, per-session URLs).
PAGE_FINDER_SKIP_HOSTS = ("youtube.com", "youtu.be", "spotify.com")


def page_finder_available(url: str = "") -> bool:
    """The headless-browser page finder is installed, switched on, and fits ``url``."""
    if tb.PAGE_FINDER == "off" or not page_sniffer.is_enabled():
        return False
    host = (urlparse(url).hostname or "").lower()
    return not any(host == h or host.endswith("." + h) for h in PAGE_FINDER_SKIP_HOSTS)


async def find_video_on_page(url: str, is_cancelled=lambda: False) -> SniffResult:
    """Open the page headlessly and return the videos its player loads."""
    try:
        return await page_sniffer.sniff_page(
            url,
            wait_seconds=tb.PAGE_FINDER_TIMEOUT,
            cookies_file=tb.cookies_file(),
            proxy=tb.YTDLP_PROXY or None,
            is_cancelled=is_cancelled,
        )
    except Exception as exc:
        text = str(exc)
        if "Executable doesn't exist" in text or "playwright install" in text:
            raise DownloadFailed(
                "The page finder's browser isn't installed. On the server run: "
                "python -m playwright install --with-deps chromium"
            ) from exc
        raise DownloadFailed(f"Couldn't open the page: {tb.shorten(text, 200)}") from exc


def no_video_reason(result: SniffResult) -> str:
    if result.drm_only:
        return "This video is DRM-protected, so it can't be downloaded."
    return (
        "No video showed up on the page. The player may need a login or an age "
        "check (add cookies in ⚙️ Settings → 🍪 Cookies), or the site blocks this server."
    )


def page_ytdlp_info(ydl, candidate: dict, page_url: str) -> dict:
    """A yt-dlp info dict for a stream found on a page, built from the playlist text
    the browser already received (re-fetching it often fails: one-time links)."""
    from yt_dlp.extractor.common import InfoExtractor
    from yt_dlp.utils import compat_etree_fromstring

    cand = Candidate.from_dict(candidate)
    headers = {k.title(): v for k, v in cand.headers.items()}
    ie = InfoExtractor(ydl)
    if cand.kind == "hls":
        formats, _ = ie._parse_m3u8_formats_and_subtitles(
            cand.manifest, cand.url, ext="mp4", m3u8_id="hls"
        )
    elif cand.kind == "dash":
        formats, _ = ie._parse_mpd_formats_and_subtitles(
            compat_etree_fromstring(cand.manifest.encode()),
            mpd_base_url=cand.url.rsplit("/", 1)[0],
            mpd_url=cand.url,
        )
    else:
        ext = Path(urlparse(cand.url).path).suffix.lstrip(".").lower() or "mp4"
        formats = [{"url": cand.url, "ext": ext, "format_id": "file"}]
    if not formats:
        raise DownloadFailed("The stream found on the page has no playable formats.")
    for fmt in formats:
        fmt["http_headers"] = {**fmt.get("http_headers", {}), **headers}
    title = str(candidate.get("title") or urlparse(page_url).netloc)
    return {
        "id": hashlib.sha1(page_url.encode()).hexdigest()[:10],
        "title": tb.shorten(title, 150),
        "formats": formats,
        "http_headers": headers,
        "webpage_url": page_url,
        "extractor": "page",
        "extractor_key": "Page",
    }


def page_picker_screen(rid: str, request: dict) -> tuple[str, InlineKeyboardMarkup]:
    """The quality picker for the video a page finder lookup picked."""
    candidates = request["candidates"]
    cand = Candidate.from_dict(candidates[request.get("index", 0)])
    host = urlparse(request["url"]).netloc.removeprefix("www.")
    lines = [
        f"🎬 <b>{html.escape(tb.shorten(request.get('title') or host, 90))}</b>",
        f"{html.escape(host)} · {html.escape(cand.describe())} · found on the page",
        "",
        "Choose a quality:",
    ]
    if cand.kind == "direct":
        actions = [("⭐ Download", "best"), ("🎵 MP3", "mp3")]
        if cand.duration is None or cand.duration <= animation.GIF_MAX_SECONDS:
            actions.append(("🎞 GIF", "gif"))
    else:
        actions = tb.quality_actions(cand.heights, cand.duration)
    if len(candidates) > 1:
        actions.append((f"🎞 Other videos ({len(candidates) - 1})", "others"))
    return "\n".join(lines), tb.link_request_markup(rid, actions)


def page_others_screen(rid: str, request: dict) -> tuple[str, InlineKeyboardMarkup]:
    candidates = [Candidate.from_dict(c) for c in request["candidates"]]
    current = request.get("index", 0)
    actions = [
        (("• " if i == current else "") + f"{i + 1}. {c.describe()}", f"pick{i}")
        for i, c in enumerate(candidates[:8])
    ]
    actions.append(("⬅ Back", "back"))
    text = (
        f"🎞 <b>{len(candidates)} videos on this page</b>\n\n"
        "The first one is the main video; the others are usually trailers, previews or ads."
    )
    return text, tb.link_request_markup(rid, actions)


async def show_page_picker(message, rid: str, url: str):
    """Fill the "looking for a video" prompt once the page finder is done."""
    reason = ""
    result: SniffResult | None = None
    try:
        result = await tb.find_video_on_page(url, is_cancelled=lambda: rid not in tb.link_requests)
    except DownloadFailed as exc:
        reason = exc.reason
    if rid not in tb.link_requests:
        return  # cancelled while we were looking
    if result is None or result.main is None:
        tb.link_requests.pop(rid, None)
        if result is not None:
            reason = tb.no_video_reason(result)
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🍪 Add cookies", callback_data="nav:cookies")]]
        )
        await tb.safe_edit_message(
            message,
            f"❌ <b>No video found</b>\n\n{html.escape(reason)}",
            markup,
            parse_mode=ParseMode.HTML,
        )
        return
    request = tb.link_requests[rid]
    request.update(
        title=result.title,
        index=0,
        candidates=[c.to_dict() for c in result.candidates[:8]],
    )
    text, markup = tb.page_picker_screen(rid, request)
    await tb.safe_edit_message(message, text, markup, parse_mode=ParseMode.HTML)


def video_format_selector(max_height: int | None) -> str:
    """yt-dlp format: best video+audio, optionally capped at ``max_height``.

    Falls back to the best available format so a cap never makes a download fail.
    """
    if not max_height:
        return "bv*+ba/b"
    h = int(max_height)
    return f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"


def is_video_url(text: str) -> bool:
    text = tb.extract_http_url(text).lower()
    return tb.is_supported_video_url(text)


def build_hentai_playlist_prompt_text(playlist: HentaiPlaylist, user_id: int = 0) -> str:
    mode = tb.get_user_settings(user_id).get("batch_download_mode")
    return (
        f"{ICON_DOWNLOAD} Hentai playlist detected\n\n"
        f"Site: {playlist.site}\n"
        f"Title:\n{tb.shorten(tb.clean_download_name(playlist.title), 100)}\n"
        f"Episodes: {len(playlist.urls)}\n\n"
        f"Batch mode: {tb.batch_download_mode_label(mode)}\n"
        f"{tb.batch_download_mode_description(mode)}\n\n"
        f"Folder:\n{tb.HENTAI_VIDEO_DIR / tb.video_platform_slug(playlist.urls[0])}"
    )


def build_pornhub_model_prompt_text(playlist: PornHubModelPlaylist, user_id: int = 0) -> str:
    mode = tb.get_user_settings(user_id).get("batch_download_mode")
    return (
        f"{ICON_DOWNLOAD} PornHub model page detected\n\n"
        f"Model:\n{tb.shorten(tb.clean_download_name(playlist.slug), 100)}\n"
        f"Videos found: {len(playlist.urls)}\n\n"
        f"Batch mode: {tb.batch_download_mode_label(mode)}\n"
        f"{tb.batch_download_mode_description(mode)}\n\n"
        f"Folder:\n{tb.ADULT_VIDEO_DIR / 'PornHub'}"
    )


def extract_spotify_url(text: str) -> str:
    match = re.search(
        r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?"
        r"(?:track|album|playlist|artist|episode|show)/[A-Za-z0-9]+(?:\?[^\s]+)?",
        text.strip(),
        re.IGNORECASE,
    )
    return match.group(0) if match else text.strip()


def build_spotify_prompt_text(url: str) -> str:
    return (
        f"{ICON_AUDIO} Spotify link detected\n\n"
        "Download this with spotDL?\n\n"
        f"Folder:\n{tb.SPOTIFY_DIR}\n\n"
        f"Link:\n{tb.shorten(url, 160)}"
    )


def build_manga_prompt_text(url: str) -> str:
    return (
        f"{ICON_IMAGE} Manga/gallery link detected\n\n"
        "Download this gallery?\n\n"
        f"Folder:\n{tb.MANGA_DIR}\n\n"
        f"Link:\n{tb.shorten(url, 160)}"
    )


async def convert_manga_folder_to_pdf_job(folder: Path, user_id: int) -> Path:
    settings = tb.get_user_settings(user_id)
    remove_images = bool(settings.get("manga_remove_images_after_conversion"))
    loop = asyncio.get_running_loop()
    pdf_path = await loop.run_in_executor(
        None,
        lambda: tb.convert_images_to_pdf(
            folder,
            tb.DOWNLOAD_DIR,
            remove_images=remove_images,
            title=folder.name,
        ),
    )
    if remove_images:
        tb.remove_manga_folder_if_empty(folder)
    return pdf_path


async def wait_for_download_slot(
    app: Application, job: dict, cancelled=None, check_disk: bool = True
) -> bool:
    """Hold a job until it may download (Settings › Downloads at once).

    While it waits its card says "Queued (position n)". False if the job was
    cancelled in the queue or, with ``check_disk``, refused for lack of disk
    space (then its status is "failed" and its card is final). The caller
    frees the slot with ``download_slots.release`` when the download ends.
    """
    user_id = job.get("user_id") or 0
    shown = {"position": None}

    async def queued(position: int | None):
        force = position != shown["position"]
        shown["position"] = position
        job["status"] = "queued"
        job["queue_position"] = position
        await tb.maybe_auto_update_status_message(app, job, force=force)

    acquired = await tb.download_slots.acquire(
        user_id,
        job["id"],
        lambda: tb.limit_of(tb.get_user_settings(user_id)),
        on_wait=queued,
        cancelled=cancelled or (lambda: job.get("status") == "cancelled"),
    )
    job.pop("queue_position", None)
    if acquired and job.get("status") == "queued":
        job["status"] = "starting"
    if acquired and check_disk and not await tb.ensure_job_disk_space(app, job):
        return False
    return acquired


async def run_with_download_slot(app: Application, job: dict, run) -> None:
    """Run ``run()`` once the job has a download slot, and free the slot after."""
    try:
        if await tb.wait_for_download_slot(app, job):
            await run()
    finally:
        tb.download_slots.release(job.get("user_id") or 0, job["id"])
