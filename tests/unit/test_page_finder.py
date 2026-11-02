"""The page finder: Video Grabber's detection rules, the picker flow, and a real run."""

import asyncio
import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

import app.bot.telegram_bot as tb
from app.services import page_sniffer as ps
from app.services import user_settings
from app.services.page_sniffer import Candidate, SniffResult
from tests.fakes import callback_update, make_context, text_update

MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nv360.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\nhttps://cdn.example/v720.m3u8\n"
)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "Download"
    root.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", root)
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "link_requests", {})
    monkeypatch.setattr(tb, "search_ui", None)
    return root


# ---------------------------------------------------------------- detection rules


@pytest.mark.parametrize(
    ("url", "status", "ctype", "rtype", "expected"),
    [
        (
            "https://s.example/a/master.m3u8",
            200,
            "application/vnd.apple.mpegurl",
            "xhr",
            ("hls", False),
        ),
        ("https://s.example/play?x=1", 200, "application/x-mpegURL", "fetch", ("hls", False)),
        ("https://s.example/hls/index", 200, "text/plain", "xhr", ("hls", True)),  # check body
        ("https://s.example/js/hls.min.js", 200, "application/javascript", "script", (None, False)),
        ("https://s.example/v/manifest.mpd", 200, "", "xhr", ("dash", True)),
        ("https://s.example/v/manifest", 200, "application/dash+xml", "xhr", ("dash", False)),
        ("https://s.example/seg/00012.ts", 200, "video/mp2t", "xhr", (None, False)),
        ("https://s.example/seg/1.m4s", 200, "video/iso.segment", "xhr", (None, False)),
        ("https://s.example/v.mp4", 200, "video/mp4", "media", ("direct", False)),
        ("https://s.example/blob", 206, "application/octet-stream", "media", ("direct", False)),
        ("https://s.example/v.mp4", 403, "video/mp4", "media", (None, False)),
        ("https://s.example/cover.jpg", 200, "image/jpeg", "image", (None, False)),
        ("https://s.example/api/hls-config", 200, "application/json", "xhr", (None, False)),
    ],
)
def test_classification_follows_video_grabber(url, status, ctype, rtype, expected):
    assert ps.classify(url, status, ctype, rtype) == expected


def test_master_playlists_list_qualities_and_hide_their_variants():
    info = ps.analyze_hls(MASTER, "https://s.example/a/master.m3u8?t=1")
    assert info["is_master"] and info["heights"] == [360, 720]
    assert info["variants"] == ["https://s.example/a/v360.m3u8", "https://cdn.example/v720.m3u8"]
    assert info["children"] == {"https://s.example/a/v360.m3u8", "https://cdn.example/v720.m3u8"}

    media = ps.analyze_hls("#EXTM3U\n#EXTINF:4.0,\na.ts\n#EXTINF:3.5,\nb.ts\n", "https://x/y.m3u8")
    assert not media["is_master"] and media["duration"] == 7.5


@pytest.mark.parametrize(
    "key_line",
    [
        '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://abc"',
        '#EXT-X-KEY:METHOD=AES-128,URI="k",KEYFORMAT="com.apple.streamingkeydelivery"',
    ],
)
def test_drm_is_refused(key_line):
    assert ps.analyze_hls(f"#EXTM3U\n{key_line}\n#EXTINF:4,\na.ts\n", "https://x/y")["drm"]
    assert not ps.analyze_hls('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n', "https://x/y")["drm"]
    assert ps.analyze_dash('<MPD><ContentProtection schemeIdUri="x"/></MPD>')["drm"]


def test_dash_qualities_and_length():
    info = ps.analyze_dash(
        '<MPD mediaPresentationDuration="PT1H2M3.5S"><Representation height="480"/>'
        '<Representation height="1080"/></MPD>'
    )
    assert info == {"heights": [480, 1080], "duration": 3723.5, "drm": False}


def test_the_main_video_comes_first():
    ad = Candidate("https://ads/x.mp4", "direct", size=4_000_000, order=1)
    preview = Candidate("https://s/p.m3u8", "hls", heights=[360], duration=30, order=2)
    single = Candidate("https://s/720.m3u8", "hls", heights=[720], duration=1500, order=3)
    master = Candidate("https://s/master.m3u8", "hls", is_master=True, heights=[720], order=4)

    assert ps.rank([ad, preview, single, master]) == [master, single, preview, ad]


def test_descriptions_and_cookies(tmp_path):
    assert (
        Candidate("u", "hls", heights=[360, 1080], duration=754).describe() == "HLS · 1080p · 12:34"
    )
    assert Candidate("u", "direct", size=50 * 1024 * 1024).describe() == "File · 50 MB"

    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n"
        "#HttpOnly_.site.example\tTRUE\t/\tTRUE\t1999999999\tsession\tabc\n"
        ".site.example\tTRUE\t/\tFALSE\t0\tage_ok\t1\nbroken line\n"
    )
    assert ps.netscape_to_playwright(cookies) == [
        {
            "name": "session",
            "value": "abc",
            "domain": ".site.example",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "expires": 1999999999,
        },
        {
            "name": "age_ok",
            "value": "1",
            "domain": ".site.example",
            "path": "/",
            "secure": False,
            "httpOnly": False,
        },
    ]
    assert ps.browser_user_agent("Mozilla/5.0 HeadlessChrome/141.0") == "Mozilla/5.0 Chrome/141.0"


def test_saved_playlist_becomes_yt_dlp_formats_with_the_player_headers():
    import yt_dlp

    candidate = {
        "title": "Episode 42",
        **Candidate(
            "https://s.example/a/master.m3u8?t=once",
            "hls",
            headers={"referer": "https://player.example/embed/42", "x-signature": "sig"},
            manifest=MASTER,
            is_master=True,
        ).to_dict(),
    }
    with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
        info = tb.page_ytdlp_info(ydl, candidate, "https://site.example/watch/42")

    assert info["title"] == "Episode 42"
    assert [f["height"] for f in info["formats"]] == [360, 720]
    assert info["formats"][0]["url"] == "https://s.example/a/v360.m3u8"
    assert info["formats"][1]["http_headers"]["Referer"] == "https://player.example/embed/42"
    assert info["formats"][1]["http_headers"]["X-Signature"] == "sig"


# ---------------------------------------------------------------- bot flow (fake browser)


def _result(*candidates, title="Episode 42"):
    return SniffResult("https://site.example/watch/42", title, list(candidates))


@pytest.fixture
def finder(monkeypatch):
    """Page finder on, with a fake browser returning ``finder.result``."""
    monkeypatch.setattr(tb, "PAGE_FINDER", "auto")
    monkeypatch.setattr(ps, "is_enabled", lambda: True)
    state = type("State", (), {"result": _result(), "calls": []})()

    async def fake_find(url, is_cancelled=lambda: False):
        state.calls.append(url)
        if isinstance(state.result, Exception):
            raise state.result
        return state.result

    monkeypatch.setattr(tb, "find_video_on_page", fake_find)
    return state


async def _drain():
    while tb.background_tasks:
        await asyncio.gather(*list(tb.background_tasks), return_exceptions=True)
        await asyncio.sleep(0)


def _buttons(call):
    markup = call.kwargs.get("reply_markup")
    return [b for row in markup.inline_keyboard for b in row] if markup else []


async def test_unknown_link_opens_the_page_and_offers_qualities(finder):
    master = Candidate(
        "https://s/m.m3u8", "hls", is_master=True, heights=[480, 1080], manifest=MASTER
    )
    trailer = Candidate("https://s/t.mp4", "direct", size=9_000_000, order=2)
    finder.result = _result(master, trailer)
    context = make_context()

    await tb.on_text(text_update(context, "https://site.example/watch/42"), context)
    assert "Looking for a video on this page" in context.bot.texts()[0]
    await _drain()

    picker = context.bot.calls[-1]
    assert "🎬 <b>Episode 42</b>" in picker.kwargs["text"]
    assert "site.example · HLS · 1080p · found on the page" in picker.kwargs["text"]
    labels = [b.text for b in _buttons(picker)]
    assert labels[:3] == ["⭐ Best (1080p)", "720p", "480p"]
    assert "🎞 Other videos (1)" in labels


async def test_other_videos_can_be_picked_without_closing_the_prompt(finder, monkeypatch):
    master = Candidate("https://s/m.m3u8", "hls", is_master=True, heights=[1080], manifest=MASTER)
    trailer = Candidate("https://s/t.mp4", "direct", size=9_000_000, order=2)
    finder.result = _result(master, trailer)
    started = []

    async def fake_ytdlp(app, chat_id, url, **kwargs):
        started.append((url, kwargs))
        job = {"id": 1, "name": "x", "status": "starting", "provider": "yt-dlp", "chat_id": chat_id}
        tb.download_jobs[1] = job
        return job

    monkeypatch.setattr(tb, "start_ytdlp_download", fake_ytdlp)
    context = make_context()
    await tb.on_text(text_update(context, "https://site.example/watch/42"), context)
    await _drain()
    rid = next(iter(tb.link_requests))

    await tb.handle_link_request_callback(callback_update(context, f"lp:{rid}:others"), context)
    assert "2 videos on this page" in context.bot.texts()[-1]
    await tb.handle_link_request_callback(callback_update(context, f"lp:{rid}:pick1"), context)
    assert "File · 9 MB" in context.bot.texts()[-1]

    await tb.handle_link_request_callback(callback_update(context, f"lp:{rid}:best"), context)

    [(url, kwargs)] = started
    assert url == "https://site.example/watch/42"
    assert kwargs["page"]["url"] == "https://s/t.mp4" and kwargs["page"]["title"] == "Episode 42"
    assert rid not in tb.link_requests


async def test_no_video_explains_why(finder):
    finder.result = SniffResult("u", "t", [], drm_only=True)
    context = make_context()

    await tb.on_text(text_update(context, "https://site.example/drm"), context)
    await _drain()

    assert "DRM-protected" in context.bot.texts()[-1]
    assert tb.link_requests == {}


async def test_known_links_never_reach_the_page_finder(finder, monkeypatch):
    monkeypatch.setattr(tb, "show_quality_picker", _noop)
    context = make_context()
    await tb.on_text(text_update(context, "https://youtu.be/abc"), context)
    await _drain()
    assert finder.calls == []
    assert not tb.page_finder_available("https://www.youtube.com/watch?v=x")


async def _noop(*args, **kwargs):
    return None


# ---------------------------------------------------------------- real browser + yt-dlp


def _chromium_ok() -> bool:
    if not ps.is_enabled():
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


REAL = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and _chromium_ok()), reason="needs ffmpeg and Playwright Chromium"
)

PAGE = b"""<!doctype html><title>Episode 42</title><h1>Episode 42</h1>
<iframe src="http://localhost:PORT/embed" width="800" height="450"></iframe>"""
# A player that loads nothing until Play is pressed, then fetches the playlist
# with a signature header (no hls.js: the test needs no internet).
EMBED = b"""<!doctype html><video id="v" width="760" height="420" muted></video>
<button id="play" style="position:absolute;inset:0">Play</button><script>
document.getElementById('play').onclick = async () => {
  const r = await fetch('/stream/master.m3u8?t=' + (await (await fetch('/token')).text()),
                        {headers: {'X-Signature': 'sig-42'}});
  await r.text();
};</script>"""


def _serve(stream_dir: Path):
    tokens: dict[str, int] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, body=b"", ctype="text/plain"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "X-Signature")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.reply(204)

        def do_GET(self):
            port = self.server.server_address[1]
            path, _, query = self.path.partition("?")
            if path == "/watch/42":
                return self.reply(200, PAGE.replace(b"PORT", str(port).encode()), "text/html")
            if path == "/embed":
                return self.reply(200, EMBED, "text/html")
            if path == "/token":
                tokens["t1"] = 2  # one-time-ish: two fetches
                return self.reply(200, b"t1")
            if not self.headers.get("Referer", "").startswith(f"http://localhost:{port}/embed"):
                return self.reply(403, b"bad referer")
            name = path.rsplit("/", 1)[-1]
            if name.endswith(".m3u8") and self.headers.get("X-Signature") != "sig-42":
                return self.reply(403, b"bad signature")
            if name == "master.m3u8":
                if tokens.get(query.removeprefix("t="), 0) <= 0:
                    return self.reply(410, b"gone")
                tokens[query.removeprefix("t=")] -= 1
            file = stream_dir / name
            if not file.is_file():
                return self.reply(404)
            ctype = "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"
            return self.reply(200, file.read_bytes(), ctype)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@REAL
async def test_real_browser_finds_the_stream_and_yt_dlp_downloads_it(
    tmp_path, isolated, monkeypatch
):
    stream = tmp_path / "stream"
    stream.mkdir()
    for h in (360, 720):
        await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size={h * 16 // 9}x{h}:rate=25:duration=6",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=6",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-g",
                "50",
                "-c:a",
                "aac",
                "-hls_time",
                "2",
                "-hls_playlist_type",
                "vod",
                "-hls_segment_filename",
                str(stream / f"v{h}_%02d.ts"),
                str(stream / f"v{h}.m3u8"),
            ],
            check=True,
        )
    (stream / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360\nv360.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720\nv720.m3u8\n"
    )
    server = _serve(stream)
    port = server.server_address[1]
    monkeypatch.setattr(tb, "PAGE_FINDER", "auto")
    monkeypatch.setattr(tb, "PAGE_FINDER_TIMEOUT", 25)
    try:
        page_url = f"http://127.0.0.1:{port}/watch/42"
        result = await tb.find_video_on_page(page_url)
        main = result.main
        assert main is not None and main.kind == "hls" and main.is_master
        assert main.headers["referer"].startswith(f"http://localhost:{port}/embed")
        assert main.headers["x-signature"] == "sig-42"
        assert result.title == "Episode 42"

        context = make_context()
        job = await tb.start_ytdlp_download(
            context.application,
            1,
            page_url,
            user_id=1,
            max_height=720,
            page={"title": result.title, **main.to_dict()},
        )
    finally:
        server.shutdown()

    assert job["status"] == "completed", job.get("last_line")
    ffprobe = await asyncio.to_thread(
        subprocess.run,
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,height",
            "-of",
            "csv=p=0",
            job["filepath"],
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    probe = ffprobe.stdout.split()
    assert "h264,720" in probe and "aac" in probe


@pytest.mark.parametrize(
    ("url", "referer", "is_ad"),
    [
        ("https://cdn.pornfhd.com/files/Top_Banner/728x90/19035.mp4", "", True),
        ("https://cdn.storagexhd.com/files/video/9202-1-300x250.medium.mp4", "", True),
        ("https://cdn.x/v.mp4", "https://cdn.x/files/banner_300x250.html?targetUrl=https%3A", True),
        ("https://poweredby.jads.co/adshow.php?adzone=847494", "", True),
        ("https://ads.example/vast/creative.mp4", "", True),
        ("https://cdn.site/hls/1280x720/index.m3u8", "https://player.site/p/abc", False),
        ("https://cdn.site/hls/640x360/index.m3u8", "", False),
        ("https://s.example/download/video.mp4", "", False),
        (
            "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8",
            "https://hlsjs.video-dev.org/demo/",
            False,
        ),
    ],
)
def test_ad_videos_are_recognised(url, referer, is_ad):
    assert ps.looks_like_ad(url, referer) is is_ad


def test_site_of_groups_subdomains():
    assert ps.site_of("https://www.javhdporn.net/video/x") == "javhdporn.net"
    assert ps.site_of("https://video1.javhdporn.net/p/x") == "javhdporn.net"
    assert ps.site_of("https://a.b.example.co.uk/x") == "example.co.uk"
    assert ps.site_of("https://stripchat.com/room") != ps.site_of("https://www.javhdporn.net/")


ADS_PAGE = b"""<!doctype html><title>Ads only</title>
<iframe src="/banner_300x250.html?targetUrl=https%3A%2F%2Ftrk.example" width="300" height="250"></iframe>"""
AD_FRAME = (
    b"""<!doctype html><video src="/files/Top_Banner/300x250/1.mp4" autoplay muted loop></video>"""
)


@REAL
async def test_a_page_with_only_ad_videos_has_no_video(tmp_path):
    clip = tmp_path / "ad.mp4"
    await asyncio.to_thread(
        subprocess.run,
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=300x250:duration=20",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            str(clip),
        ],
        check=True,
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            body, ctype = {
                "/watch": (ADS_PAGE, "text/html"),
                "/banner_300x250.html": (AD_FRAME, "text/html"),
                "/files/Top_Banner/300x250/1.mp4": (clip.read_bytes(), "video/mp4"),
            }.get(path, (b"", "text/plain"))
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = await ps.sniff_page(
            f"http://127.0.0.1:{server.server_address[1]}/watch", wait_seconds=8
        )
    finally:
        server.shutdown()

    assert result.candidates == []  # the banner video is never offered as "the video"


# The first press of Play does a "tab-under": the page opens itself in a new tab and
# sends this tab to an ad on another site. Only the second press starts the player.
TAB_UNDER_PAGE = b"""<!doctype html><title>Tab-under</title>
<button id="play" style="position:absolute;inset:0">Play</button><script>
let presses = 0;
document.getElementById('play').onclick = async () => {
  presses += 1;
  if (presses === 1) { window.open(location.href); location.href = 'http://localhost:PORT/ad'; return; }
  await (await fetch('/stream/master.m3u8')).text();
};</script>"""


@REAL
async def test_tab_under_redirects_are_blocked_and_play_is_pressed_again():
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            port = self.server.server_address[1]
            if self.path == "/watch":
                body, ctype = TAB_UNDER_PAGE.replace(b"PORT", str(port).encode()), "text/html"
            elif self.path == "/stream/master.m3u8":
                body, ctype = MASTER.encode(), "application/vnd.apple.mpegurl"
            else:
                body, ctype = b"ad", "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = await ps.sniff_page(
            f"http://127.0.0.1:{server.server_address[1]}/watch", wait_seconds=15
        )
    finally:
        server.shutdown()

    assert result.main is not None and result.main.url.endswith("/stream/master.m3u8")
    assert "/ad" not in hits  # the redirect never left the browser
