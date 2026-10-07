"""Cookies sent to the bot, the cobalt fallback, and GIFs sent as GIFs."""

import json
import shutil
import stat
import subprocess
from types import SimpleNamespace

import httpx
import pytest

import app.bot.telegram_bot as tb
from app.services import animation, cookies, user_settings
from app.services.cobalt import CobaltClient, CobaltError, CobaltItem, parse_response
from tests.fakes import FakeMessage, callback_update, make_context

COOKIE_LINE = ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc"
HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "Download"
    root.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", root)
    monkeypatch.setattr(tb, "UPLOADED_COOKIES_PATH", tmp_path / "data" / "cookies.txt")
    monkeypatch.setattr(tb, "YTDLP_COOKIES_FILE", "")
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "status_messages", {})
    return root


# --- cookies ----------------------------------------------------------------


def test_cookie_files_are_validated_and_given_a_header():
    text, summary = cookies.parse_cookies(
        f"{COOKIE_LINE}\r\n#HttpOnly_.google.com\tTRUE\t/\tTRUE\t0\tA\tb\n".encode()
    )
    assert text.startswith("# Netscape HTTP Cookie File\n")
    assert summary.count == 2 and summary.sites == ["youtube.com", "google.com"]

    with pytest.raises(cookies.CookieFileError, match="JSON"):
        cookies.parse_cookies(b'[{"name": "SID"}]')
    with pytest.raises(cookies.CookieFileError, match="No cookies"):
        cookies.parse_cookies(b"hello")


def test_saved_cookies_are_private(tmp_path):
    path = tmp_path / "d" / "cookies.txt"
    cookies.save_cookies(path, "# Netscape HTTP Cookie File\n" + COOKIE_LINE + "\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert cookies.read_summary(path).sites == ["youtube.com"]


class DocMessage(FakeMessage):
    def __init__(self, bot, data: bytes, name: str):
        super().__init__(bot, 1, None, user_id=1)
        self.deleted = False
        file = SimpleNamespace(download_as_bytearray=self._bytes(data))
        self.document = SimpleNamespace(
            file_name=name, file_size=len(data), get_file=self._file(file)
        )
        self.chat = SimpleNamespace(id=1, send_message=self._send)

    @staticmethod
    def _bytes(data):
        async def download():
            return bytearray(data)

        return download

    @staticmethod
    def _file(file):
        async def get_file():
            return file

        return get_file

    async def _send(self, text, **kwargs):
        return await self.reply_text(text, **kwargs)

    async def delete(self):
        self.deleted = True


async def test_uploaded_cookies_are_saved_and_the_message_deleted():
    context = make_context()
    await tb.on_button(callback_update(context, "ck:send"), context)
    assert "Send the cookies.txt file now" in context.bot.texts()[-1]

    message = DocMessage(context.bot, COOKIE_LINE.encode(), "export.txt")
    await tb.on_document(
        SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1)), context
    )

    assert message.deleted
    assert tb.cookies_file() == str(tb.UPLOADED_COOKIES_PATH)
    assert "Saved 1 cookies for youtube.com" in context.bot.texts()[-1]
    assert "cookies_wait" not in context.user_data
    assert tb.ytdlp_common_options("https://youtu.be/x", SimpleNamespace(referer=None))[
        "cookiefile"
    ] == str(tb.UPLOADED_COOKIES_PATH)

    await tb.on_button(callback_update(context, "ck:dely"), context)
    assert tb.cookies_file() is None


async def test_other_files_are_ignored_when_not_waiting_for_cookies():
    context = make_context()
    message = DocMessage(context.bot, b"%PDF", "book.pdf")
    await tb.on_document(
        SimpleNamespace(message=message, effective_user=SimpleNamespace(id=1)), context
    )
    assert context.bot.calls == [] and not message.deleted


def test_cookie_failures_offer_the_cookies_screen():
    from app.bot.views import jobs as job_views

    reason, needs = tb.explain_spotdl_error(
        "AudioProviderError: YT-DLP download error - https://www.youtube.com/watch?v=x"
    )
    assert needs and "Settings → 🍪 Cookies" in reason
    job = {"id": 1, "name": "x", "status": "failed", "provider": "spotify", "needs_cookies": True}
    _, markup = job_views.job_card(job)
    assert markup.inline_keyboard[0][0].callback_data == "nav:cookies"


# --- cobalt -----------------------------------------------------------------


def test_cobalt_responses():
    [item] = parse_response({"status": "redirect", "url": "https://v/a.mp4", "filename": "a.mp4"})
    assert item == CobaltItem("https://v/a.mp4", "a.mp4", "video")

    items = parse_response(
        {"status": "picker", "picker": [{"type": "photo", "url": "p"}, {"type": "gif", "url": "g"}]}
    )
    assert [(i.filename, i.kind) for i in items] == [
        ("item_01.jpg", "photo"),
        ("item_02.mp4", "gif"),
    ]

    with pytest.raises(CobaltError, match="private"):
        parse_response({"status": "error", "error": {"code": "error.api.content.video.private"}})


async def test_cobalt_client_requests_and_downloads(tmp_path):
    seen = {}

    def handler(request: httpx.Request):
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(
                200, json={"status": "tunnel", "url": "http://c/tunnel", "filename": "clip.mp4"}
            )
        return httpx.Response(200, content=b"x" * 1000)

    client = CobaltClient("http://c", "key", transport=httpx.MockTransport(handler))
    [item] = await client.resolve("https://x.com/a/status/1", mute=True, max_height=720)
    path = await client.download(item, tmp_path)

    assert seen["body"]["downloadMode"] == "mute" and seen["body"]["videoQuality"] == "720"
    assert seen["auth"] == "Api-Key key"
    assert path.read_bytes() == b"x" * 1000


async def test_empty_cobalt_tunnels_are_errors(tmp_path):
    client = CobaltClient("http://c", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    target = tmp_path / "out"
    with pytest.raises(CobaltError, match="empty"):
        await client.download(CobaltItem("http://c/t", "a.mp4", "video"), target)
    assert list(target.iterdir()) == []


class FakeCobalt:
    enabled = True

    def __init__(self, items=None, error=None):
        self.items, self.error = items or [], error

    async def resolve(self, url, **kwargs):
        if self.error:
            raise self.error
        return self.items

    async def download(self, item, destination, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / item.filename
        path.write_bytes(b"data")
        return path


async def test_cobalt_takes_over_when_ytdlp_fails(isolated, monkeypatch):
    monkeypatch.setattr(tb, "cobalt_client", FakeCobalt([CobaltItem("u", "clip.mp4", "video")]))
    job = {"id": 5, "platform": "X / Twitter"}

    title, path = await tb.download_with_cobalt(
        job, "u", isolated, RuntimeError("boom"), lambda: False
    )

    assert title == "clip" and path.endswith("clip.mp4")
    assert job["note"] == "via cobalt" and job["completed_length"] == 4


async def test_cobalt_failures_explain_both_attempts(isolated, monkeypatch):
    bot_check = "ERROR: [youtube] a: Sign in to confirm you’re not a bot."
    monkeypatch.setattr(
        tb, "cobalt_client", FakeCobalt(error=CobaltError("error.api.youtube.login"))
    )
    with pytest.raises(tb.DownloadFailed) as failed:
        await tb.download_with_cobalt(
            {"id": 1}, "u", isolated, RuntimeError(bot_check), lambda: False
        )
    assert "blocking downloads" in failed.value.reason and "cobalt: YouTube" in failed.value.reason

    monkeypatch.setattr(
        tb, "cobalt_client", FakeCobalt(error=CobaltError("error.api.link.unsupported"))
    )
    with pytest.raises(tb.DownloadFailed) as failed:
        await tb.download_with_cobalt({"id": 1}, "u", isolated, RuntimeError("Nope"), lambda: False)
    assert failed.value.reason == "Nope"


# --- GIFs -------------------------------------------------------------------


def _clip(path, *, audio: bool, seconds: int = 2):
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=321x241:duration={seconds}",
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=duration={seconds}", "-shortest"]
    subprocess.run(cmd + ["-c:v", "libx264", "-pix_fmt", "yuv444p", str(path)], check=True)
    return path


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg")
async def test_silent_clips_are_sent_as_gifs_and_videos_are_not(isolated):
    folder = isolated / "Gallery" / "reddit"
    folder.mkdir(parents=True)
    _clip(folder / "silent.mp4", audio=False)
    _clip(folder / "talking.mp4", audio=True)
    job = {
        "id": 7,
        "name": "post",
        "status": "completed",
        "provider": "gallery-dl",
        "chat_id": 1,
        "user_id": 1,
        "outputs": [str(folder)],
    }
    context = make_context()

    await tb.send_job_gifs(context.application, job)

    [call] = context.bot.called("send_animation")
    assert call.kwargs["width"] % 2 == 0 and call.kwargs["duration"] == 2
    assert job["gif_note"] == "🎞 Sent as a GIF above"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg")
async def test_gif_jobs_mute_and_cut_long_videos(isolated, monkeypatch):
    monkeypatch.setattr(animation, "GIF_MAX_SECONDS", 1)
    clip = _clip(isolated / "talking.mp4", audio=True, seconds=3)
    job = {
        "id": 8,
        "name": "clip",
        "status": "completed",
        "provider": "yt-dlp",
        "gif": True,
        "chat_id": 1,
        "user_id": 1,
        "filepath": str(clip),
    }
    context = make_context()

    await tb.send_job_gifs(context.application, job)

    assert len(context.bot.called("send_animation")) == 1
    assert "first 1 s only" in job["gif_note"]


async def test_gifs_respect_the_setting(isolated):
    await user_settings.update_setting(1, "send_gifs_to_chat", False)
    job = {"id": 9, "status": "completed", "provider": "yt-dlp", "chat_id": 1, "user_id": 1}
    context = make_context()
    await tb.send_job_gifs(context.application, job)
    assert context.bot.calls == [] and "gif_note" not in job
