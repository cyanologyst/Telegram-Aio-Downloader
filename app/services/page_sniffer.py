"""Find the video a web page plays, without anyone pressing Play.

The last resort for links nothing else understands: the page is opened in a
headless Chromium (Playwright), its player is started the way a person would
start it (the player's play button, ``video.play()`` in every frame, a click
in the middle of the player), and every network response is checked with the
rules of the Video Grabber browser extension:

- HLS/DASH by content type, or by a URL hint (``m3u8``, ``hls``, ``.mpd``)
  confirmed by reading the body; segments are ignored;
- direct files by media content type or extension, or anything a ``<video>``
  element loaded; files under 256 KB are ignored (ad blips, previews);
- variant playlists are folded under their master;
- the headers the player sent (Referer, Origin, Cookie, Authorization,
  ``X-…``) are kept, since embedded players often need their own Referer;
- the playlist text is kept, because links often expire after one use;
- DRM-protected streams are refused.

The result is handed to yt-dlp (see ``ytdlp_info`` in the bot), which does the
quality selection, AES-128 decryption and merging.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

MIN_DIRECT_SIZE = 256 * 1024
SEGMENT_TYPES = {"video/mp2t", "video/iso.segment", "audio/iso.segment"}
SEGMENT_EXTS = {"ts", "m4s", "m4f", "m2ts", "cmfv", "cmfa", "cmft"}
DIRECT_EXTS = {"mp4", "m4v", "webm", "mkv", "mov", "flv", "ogv"}
GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream", "text/plain"}
NOT_MEDIA_TYPES = re.compile(
    r"^(text/(html|css|javascript)|image/|font/|application/(javascript|x-javascript|wasm|json))"
)
# Playwright resource types a player fetches manifests and media with.
STREAM_REQUEST_TYPES = {"xhr", "fetch", "media", "document", "other"}
# Ad creatives: standard banner sizes, click-tracking redirects, ad-server paths.
_AD_SIZES = (
    "300x250|728x90|160x600|300x600|320x50|320x100|468x60|970x90|970x250|336x280|"
    "120x600|250x250|200x200|300x100"
)
AD_HINT = re.compile(
    rf"(?<!\d)(?:{_AD_SIZES})(?!\d)|targetUrl=|[/._-](?:ads?|adserver|adshow|adzone|banners?|"
    r"vast|vpaid|preroll|popunder|sponsor)(?:[/._?=&-]|$)|//(?:ads?|adserver|track|trk|click)[.-]",
    re.I,
)
REPLAY_HEADERS = re.compile(r"^(referer|origin|cookie|authorization|x-[\w-]+)$", re.I)

PLAY_SELECTORS = (
    ".vjs-big-play-button",
    ".jw-icon-display",
    ".jw-display-icon-container",
    ".plyr__control--overlaid",
    ".fp-play",
    ".mejs__overlay-button",
    ".ytp-large-play-button",
    "[class*=play-button]",
    "[class*=playButton]",
    "[class*=play_button]",
    "button[aria-label*=play i]",
    "[title*=play i]",
    "#play",
    ".play",
)
PLAY_JS = """() => {
  for (const v of document.querySelectorAll('video,audio')) {
    v.muted = true;
    try { const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {}
  }
}"""
SCAN_JS = """() => [...document.querySelectorAll('video,audio,source')]
  .map(e => e.currentSrc || e.src).filter(u => /^https?:/.test(u || ''))"""
TITLE_JS = """() => (document.querySelector('meta[property="og:title"]') || {}).content
  || document.title || ''"""


@dataclass
class Candidate:
    url: str
    kind: str  # "hls" / "dash" / "direct"
    headers: dict[str, str] = field(default_factory=dict)
    manifest: str = ""  # the playlist text as the player received it
    is_master: bool = False
    heights: list[int] = field(default_factory=list)
    duration: float | None = None
    size: int = 0
    drm: bool = False
    order: int = 0  # when it was found

    def describe(self) -> str:
        """ "HLS · 1080p · 12:34" for buttons."""
        parts = [{"hls": "HLS", "dash": "DASH"}.get(self.kind, "File")]
        if self.heights:
            parts.append(f"{max(self.heights)}p")
        if self.duration:
            minutes, seconds = divmod(int(self.duration), 60)
            parts.append(f"{minutes}:{seconds:02d}")
        elif self.size:
            parts.append(f"{self.size / 1024 / 1024:.0f} MB")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Candidate:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SniffResult:
    page_url: str
    title: str
    candidates: list[Candidate]  # best first; DRM-protected ones are left out
    drm_only: bool = False  # streams were found, but all of them are DRM-protected

    @property
    def main(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


def looks_like_ad(url: str, referer: str = "") -> bool:
    """An ad video (or one loaded inside an ad's frame), not the page's own player."""
    return bool(AD_HINT.search(url) or (referer and AD_HINT.search(referer)))


def site_of(url: str) -> str:
    """The registrable-ish domain: "video.example.co.uk" -> "example.co.uk"."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    parts = host.split(".")
    keep = 3 if len(parts) >= 3 and len(parts[-1]) == 2 and len(parts[-2]) <= 3 else 2
    return ".".join(parts[-keep:])


def is_enabled() -> bool:
    """Playwright is installed (the browser itself is checked when launching)."""
    return importlib.util.find_spec("playwright") is not None


def _ext(url: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def classify(url: str, status: int, content_type: str | None, resource_type: str | None):
    """Video Grabber's rules: ("hls" | "dash" | "direct" | None, body needs checking)."""
    if not 200 <= status <= 299 or not url.lower().startswith(("http://", "https://")):
        return None, False
    ctype = (content_type or "").split(";")[0].strip().lower()
    ext = _ext(url)
    if ctype in SEGMENT_TYPES or ext in SEGMENT_EXTS:
        return None, False
    if "mpegurl" in ctype:
        return "hls", False
    if "dash+xml" in ctype:
        return "dash", False
    stream_request = not resource_type or resource_type in STREAM_REQUEST_TYPES
    if (
        stream_request
        and not NOT_MEDIA_TYPES.match(ctype)
        and not ctype.startswith(("video/", "audio/"))
    ):
        if ext == "m3u8" or re.search(r"m3u8|hls", url, re.I):
            return "hls", True
        if ext == "mpd" or re.search(r"\.mpd(\?|$)", url, re.I):
            return "dash", True
    is_media_type = ctype.startswith("video/")
    is_media_ext = ext in DIRECT_EXTS and ctype in GENERIC_TYPES
    is_media_element = resource_type == "media" and not NOT_MEDIA_TYPES.match(ctype)
    if is_media_type or is_media_ext or is_media_element:
        return "direct", False
    return None, False


def analyze_hls(text: str, url: str) -> dict[str, Any]:
    """Master or media playlist: qualities, length, DRM, and the playlists it points to."""
    is_master = "#EXT-X-STREAM-INF" in text
    heights = sorted({int(h) for h in re.findall(r"RESOLUTION=\d+x(\d+)", text)}, reverse=False)
    duration = sum(float(d) for d in re.findall(r"#EXTINF:([\d.]+)", text)) or None
    drm = bool(
        re.search(r"#EXT-X-(?:SESSION-)?KEY:[^\n]*METHOD=SAMPLE-AES", text)
        or re.search(r'KEYFORMAT="(?!identity")[^"]+"', text)
    )
    children = set()
    variants: list[str] = []  # the video qualities (#EXT-X-STREAM-INF), in order
    if is_master:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                variants.append(urljoin(url, line))
        children.update(variants)
        children.update(urljoin(url, m) for m in re.findall(r'URI="([^"]+)"', text))
    return {
        "is_master": is_master,
        "variants": variants,
        "heights": heights,
        "duration": duration,
        "drm": drm,
        "children": children,
    }


def _iso_duration(value: str) -> float | None:
    match = re.fullmatch(
        r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?", value.strip() or "x"
    )
    if not match:
        return None
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds or None


def analyze_dash(text: str) -> dict[str, Any]:
    heights = sorted({int(h) for h in re.findall(r'<Representation[^>]*\sheight="(\d+)"', text)})
    match = re.search(r'mediaPresentationDuration="([^"]+)"', text)
    return {
        "heights": heights,
        "duration": _iso_duration(match.group(1)) if match else None,
        "drm": "<ContentProtection" in text,
    }


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Main video first: streams before files, masters before single qualities,
    then the highest quality, the longest, the biggest, the first found."""

    def key(c: Candidate):
        return (
            c.kind == "direct",
            not c.is_master,
            -(max(c.heights) if c.heights else 0),
            -(c.duration or 0),
            -c.size,
            c.order,
        )

    return sorted(candidates, key=key)


def netscape_to_playwright(path: str | Path) -> list[dict[str, Any]]:
    """cookies.txt lines as Playwright cookies (bad lines are skipped)."""
    cookies = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for raw in text.splitlines():
        http_only = raw.startswith("#HttpOnly_")
        line = raw[len("#HttpOnly_") :] if http_only else raw
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain, _, path_, secure, expires, name, value = fields
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path_ or "/",
            "secure": secure.upper() == "TRUE",
            "httpOnly": http_only,
        }
        if expires.isdigit() and int(expires) > 0:
            cookie["expires"] = int(expires)
        cookies.append(cookie)
    return cookies


def browser_user_agent(user_agent: str) -> str:
    """Sites treat "HeadlessChrome" as a bot; present the same browser as plain Chrome."""
    return user_agent.replace("HeadlessChrome", "Chrome")


_browser_lock = asyncio.Lock()  # one browser at a time: it needs a few hundred MB of RAM


async def sniff_page(
    url: str,
    *,
    wait_seconds: float = 30,
    cookies_file: str | None = None,
    proxy: str | None = None,
    is_cancelled=lambda: False,
) -> SniffResult:
    """Open ``url`` headlessly, start its player, and return the videos it loaded."""
    from playwright.async_api import async_playwright  # optional dependency

    async with _browser_lock, async_playwright() as pw:
        launch: dict[str, Any] = {
            "args": [
                "--autoplay-policy=no-user-gesture-required",
                "--mute-audio",
                "--disable-blink-features=AutomationControlled",
            ]
        }
        if proxy:
            launch["proxy"] = {"server": proxy}
        browser = await pw.chromium.launch(**launch)
        try:
            return await _sniff(browser, url, wait_seconds, cookies_file, is_cancelled)
        finally:
            with contextlib.suppress(Exception):
                await browser.close()


async def _sniff(browser, url, wait_seconds, cookies_file, is_cancelled) -> SniffResult:
    probe = await browser.new_page()
    user_agent = browser_user_agent(await probe.evaluate("navigator.userAgent"))
    await probe.close()
    context = await browser.new_context(
        user_agent=user_agent,
        service_workers="allow",
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    if cookies_file:
        with contextlib.suppress(Exception):
            await context.add_cookies(netscape_to_playwright(cookies_file))

    found: dict[str, Candidate] = {}
    children: set[str] = set()
    master_of: dict[str, str] = {}  # quality/audio/subtitle playlist -> its master
    variants_of: dict[str, list[str]] = {}  # master -> its video quality playlists
    pending: set[asyncio.Task] = set()
    counter = iter(range(1, 1_000_000))
    main_page = None

    async def on_response(response) -> None:
        request = response.request
        kind, _ = classify(
            response.url,
            response.status,
            response.headers.get("content-type"),
            request.resource_type,
        )
        if not kind or response.url in found:
            return
        candidate = Candidate(url=response.url, kind=kind, order=next(counter))
        if kind == "hls":
            try:
                body = await response.text()
            except Exception:
                return
            if not body.lstrip().startswith("#EXTM3U"):
                return  # a URL hint that wasn't a playlist (hls.min.js, configs)
            info = analyze_hls(body, response.url)
            variants_of[response.url] = info.pop("variants")
            for child in info.pop("children"):
                children.add(child)
                master_of[child] = response.url
            candidate.manifest = body
            candidate.is_master = info["is_master"]
            candidate.heights = info["heights"]
            candidate.duration = info["duration"]
            candidate.drm = info["drm"]
        elif kind == "dash":
            try:
                body = await response.text()
            except Exception:
                return
            if "<MPD" not in body:
                return
            info = analyze_dash(body)
            candidate.manifest = body
            candidate.is_master = True
            candidate.heights = info["heights"]
            candidate.duration = info["duration"]
            candidate.drm = info["drm"]
        else:
            headers = response.headers
            total = re.search(r"/(\d+)\s*$", headers.get("content-range", ""))
            size = int(total.group(1)) if total else int(headers.get("content-length") or 0)
            if size and size < MIN_DIRECT_SIZE:
                return
            candidate.size = size
        try:
            sent = await request.all_headers()
        except Exception:
            sent = {}
        candidate.headers = {k.lower(): v for k, v in sent.items() if REPLAY_HEADERS.match(k)}
        candidate.headers["user-agent"] = user_agent
        found[response.url] = candidate

    def track(coro) -> None:
        task = asyncio.ensure_future(coro)
        pending.add(task)
        task.add_done_callback(pending.discard)

    def on_page(page) -> None:
        if main_page is not None and page is not main_page:
            track(page.close())  # pop-up ads opened by the first click

    context.on("response", lambda response: track(on_response(response)))
    context.on("page", on_page)

    main_page = await context.new_page()
    try:
        await main_page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except Exception as exc:
        if not found:
            raise RuntimeError(f"The page didn't open: {exc}") from exc
    title = ""

    deadline = time.monotonic() + wait_seconds
    clicks = 0
    rounds = 0
    home = site_of(url)
    ad_sites: set[str] = set()  # sites the page sent us to instead of playing

    def is_ad(candidate: Candidate) -> bool:
        referer_site = site_of(candidate.headers.get("referer", ""))
        return _is_ad(candidate) or referer_site in ad_sites

    while time.monotonic() < deadline and not is_cancelled():
        if site_of(main_page.url) not in (home, ""):
            # "Tab-under": the click sent this tab to an ad (and the video page to a
            # new tab). Note the ad site, come back, and press play again.
            ad_sites.add(site_of(main_page.url))
            with contextlib.suppress(Exception):
                await main_page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            continue
        # Ads (banner videos start on their own) never count as "found".
        real = [c for c in found.values() if not is_ad(c) and c.url not in children]
        streams = [c for c in real if c.kind != "direct"]
        # Small files can be hover previews; keep starting the player for a real one.
        files = [c for c in real if c.kind == "direct" and (not c.size or c.size >= 5 << 20)]
        if streams or files:
            newest = max(c.order for c in found.values())
            # Give the player a moment to fetch the master/other qualities, then stop.
            await asyncio.sleep(3 if streams else 6)
            if max(c.order for c in found.values()) == newest:
                break
            continue
        for frame in main_page.frames:
            if frame is main_page.main_frame or not looks_like_ad(frame.url):
                await _start_player(frame)
        # A real click on the player; the first one often only opens a pop-up ad.
        if clicks < 3 and rounds % 2 == 0 and await _click_player(main_page):
            clicks += 1
        rounds += 1
        await asyncio.sleep(1.5)

    for frame in list(main_page.frames):
        # Files the browser served from its memory cache never reach the network.
        with contextlib.suppress(Exception):
            for src in await frame.evaluate(SCAN_JS):
                if src not in found and src not in children and not src.startswith("blob:"):
                    found[src] = Candidate(
                        url=src,
                        kind="direct",
                        headers={"referer": frame.url, "user-agent": user_agent},
                        order=next(counter),
                    )
    with contextlib.suppress(Exception):
        title = (await main_page.evaluate(TITLE_JS)).strip()
    if pending:
        await asyncio.wait(pending, timeout=5)
    await _check_unplayed_variants(context, found, variants_of)
    with contextlib.suppress(Exception):
        await context.close()

    # DRM keys usually sit in the quality playlists the player fetched, not the master.
    for child_url, candidate in found.items():
        master = found.get(master_of.get(child_url, ""))
        if candidate.drm and master is not None:
            master.drm = True
    usable = [c for u, c in found.items() if u not in children and not is_ad(c)]
    drm = [c for c in usable if c.drm]
    # Files from the server of a DRM-protected stream are its encrypted pieces.
    drm_hosts = {urlparse(c.url).hostname for c in drm}
    usable = [
        c
        for c in usable
        if not c.drm and not (c.kind == "direct" and urlparse(c.url).hostname in drm_hosts)
    ]
    return SniffResult(
        page_url=url,
        title=title or urlparse(url).netloc,
        candidates=rank(usable),
        drm_only=bool(drm) and not usable,
    )


async def _check_unplayed_variants(context, found: dict[str, Candidate], variants_of) -> None:
    """DRM keys sit in the quality playlists. When the player never fetched any of a
    master's (it played another format), fetch one, with the player's headers."""
    for candidate in [c for c in found.values() if c.kind == "hls" and c.is_master][:3]:
        variants = variants_of.get(candidate.url, [])
        if not variants or any(v in found for v in variants):
            continue  # a quality the player fetched was already checked
        try:
            response = await context.request.get(
                variants[0], headers=candidate.headers, timeout=10_000
            )
            body = await response.text()
        except Exception:
            continue
        if body.lstrip().startswith("#EXTM3U") and analyze_hls(body, variants[0])["drm"]:
            candidate.drm = True


async def _start_player(frame) -> None:
    for selector in PLAY_SELECTORS:
        try:
            button = frame.locator(selector).first
            if await button.is_visible(timeout=150):
                await button.click(timeout=1000)
                break
        except Exception:
            continue
    with contextlib.suppress(Exception):
        await frame.evaluate(PLAY_JS)


def _is_ad(candidate: Candidate) -> bool:
    return looks_like_ad(candidate.url, candidate.headers.get("referer", ""))


# The biggest visible player-looking box that isn't an ad frame; its center, in page pixels.
PLAYER_BOX_JS = """(adPattern) => {
  const ad = new RegExp(adPattern, 'i');
  let best = null, bestArea = 0;
  const els = document.querySelectorAll(
    'video, iframe, [class*=player], [id*=player], [class*=play-button], [class*=poster]');
  for (const el of els) {
    if (el.tagName === 'IFRAME' && ad.test(el.src || '')) continue;
    const r = el.getBoundingClientRect();
    const area = r.width * r.height;
    if (r.width < 200 || r.height < 100 || r.bottom < 0 || r.top > innerHeight) continue;
    if (area > bestArea) { bestArea = area; best = r; }
  }
  return best && {x: best.x + best.width / 2, y: best.y + best.height / 2};
}"""


async def _click_player(page) -> bool:
    """A real click in the middle of the player (works on players in iframes too)."""
    try:
        point = await page.evaluate(PLAYER_BOX_JS, AD_HINT.pattern)
    except Exception:
        return False
    if not point:
        return False
    with contextlib.suppress(Exception):
        await page.mouse.click(point["x"], point["y"])
        return True
    return False
