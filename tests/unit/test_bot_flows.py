"""Regression tests for the bot's button and message flows."""

import asyncio
import time

import pytest
from pyrogram import StopTransmission

import app.bot.telegram_bot as tb
from tests.fakes import callback_update, make_context, text_update


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "status_messages", {})
    monkeypatch.setattr(tb, "link_requests", {})
    monkeypatch.setattr(tb, "torrent_select_sessions", {})
    monkeypatch.setattr(tb, "upload_jobs", {})
    monkeypatch.setattr(tb.flood_guard, "_blocked_until", {})
    monkeypatch.setattr(tb.flood_guard, "_held", {})
    monkeypatch.setattr(tb.flood_guard, "_announced", {})
    monkeypatch.setattr(tb.flood_guard, "_sends_open", set())
    monkeypatch.setattr(tb.flood_guard, "_last_probe", {})
    monkeypatch.setattr(tb.flood_guard, "_state_path", None)
    monkeypatch.setattr(tb, "_held_flushers", {})
    return tmp_path


async def _drain_background():
    while tb.background_tasks:
        await asyncio.gather(*list(tb.background_tasks), return_exceptions=True)
        await asyncio.sleep(0)  # let done-callbacks remove finished tasks


def _buttons(call):
    markup = call.kwargs.get("reply_markup")
    return [b for row in markup.inline_keyboard for b in row] if markup else []


# ---------------------------------------------------------------- answering


async def test_cancel_from_status_card_answers_once_and_keeps_the_card():
    tb.download_jobs[5] = {"id": 5, "name": "clip", "provider": "yt-dlp", "status": "downloading"}
    context = make_context()
    update = callback_update(context, "job_cancel:5")

    await tb.on_button(update, context)

    assert update.callback_query.answers == [
        {"text": "Cancelled job #5: clip", "show_alert": False}
    ]
    assert tb.download_jobs[5]["status"] == "cancelled"
    assert not any("Error" in text for text in context.bot.texts())


async def test_stale_file_button_shows_a_popup_instead_of_breaking_the_screen():
    context = make_context()
    update = callback_update(context, "fb:file:0:p999999")

    await tb.on_button(update, context)

    answer = update.callback_query.answers[0]
    assert answer["show_alert"] is True and "menu is too old" in answer["text"]
    assert context.bot.texts() == []


# ---------------------------------------------------------------- uploads


async def test_upload_cancel_stops_the_transfer_and_skips_remaining_files(monkeypatch):
    for name in ("a.bin", "b.bin"):
        (tb.DOWNLOAD_DIR / name).write_bytes(b"x" * 10)
    sent = []

    async def fake_send(rel_path, progress_callback=None, chat_id="me", **_):
        sent.append(rel_path)
        await progress_callback(1, 10)
        tb.cancel_upload(next(iter(tb.upload_jobs)))
        await progress_callback(5, 10)  # raises StopTransmission once cancelled

    async def logged_in():
        return object()

    monkeypatch.setattr(tb, "pyrogram_send_file", fake_send)
    monkeypatch.setattr(tb, "get_pyrogram_client", logged_in)
    context = make_context()

    job = await tb.upload_files_via_pyrogram(
        context.application, 1, 77, ["a.bin", "b.bin"], title="2 selected file(s)"
    )

    assert job["status"] == "cancelled"
    assert [tb.Path(path).name for path in sent] == ["a.bin"]
    final = context.bot.called("edit_message_text")[-1].kwargs
    assert final["text"].startswith("🛑 Upload cancelled")
    assert "Uploaded before cancelling: 0/2" in final["text"]


async def test_progress_shows_a_cancel_button():
    tb.upload_jobs["upload_9"] = {"cancelled": False, "last_update": 0}
    context = make_context()

    await tb.update_upload_progress(context.application, 1, 2, "upload_9", "a.bin", 5, 10)

    call = context.bot.called("edit_message_text")[-1]
    assert [b.callback_data for b in _buttons(call)] == ["up_cancel:upload_9"]

    tb.upload_jobs["upload_9"]["cancelled"] = True
    with pytest.raises(StopTransmission):
        await tb.update_upload_progress(context.application, 1, 2, "upload_9", "a.bin", 6, 10)


async def test_file_upload_button_runs_in_the_background(monkeypatch):
    (tb.DOWNLOAD_DIR / "movie.mkv").write_bytes(b"x")
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_upload(app, chat_id, message_id, rel_path, user_id=None):
        started.set()
        await release.wait()

    monkeypatch.setattr(tb, "send_single_file_via_pyrogram", slow_upload)
    context = make_context()
    token = tb.encode_path("movie.mkv")
    update = callback_update(context, f"fb:send_yes:0:{token}")

    await tb.on_button(update, context)  # returns while the upload is still running
    await asyncio.sleep(0)  # let the background task start

    assert started.is_set() and not release.is_set()
    assert len(update.callback_query.answers) == 1
    release.set()
    await _drain_background()


# ---------------------------------------------------------------- conversion


async def test_conversion_button_passes_the_chosen_resolution(monkeypatch):
    (tb.DOWNLOAD_DIR / "clip.mp4").write_bytes(b"x")
    calls = []

    async def fake_convert(src, dst, res, progress_callback=None):
        calls.append(res)
        tb.Path(dst).write_bytes(b"converted")

    monkeypatch.setattr(tb, "convert_video_quality", fake_convert)
    context = make_context()
    token = tb.encode_path("clip.mp4")

    await tb.on_button(callback_update(context, f"fb:conv_start:0:{token}:360p"), context)
    await _drain_background()

    assert calls == ["360p"]
    assert (tb.DOWNLOAD_DIR / "clip_360p.mp4").exists()

    # Converting again asks first, and "Convert again" really re-encodes.
    await tb.on_button(callback_update(context, f"fb:conv_start:0:{token}:360p"), context)
    assert "already exists" in context.bot.texts()[-1]
    await tb.on_button(callback_update(context, f"fb:conv_start:0:{token}:360p!"), context)
    await _drain_background()
    assert calls == ["360p", "360p"]


def test_ffmpeg_progress_parsing():
    assert tb.parse_ffmpeg_duration("  Duration: 01:02:03.50, start: 0.0") == 3723.5
    assert tb.parse_ffmpeg_duration("no duration here") is None
    assert tb.parse_ffmpeg_progress_seconds("out_time_us=2500000\n") == 2.5
    assert tb.parse_ffmpeg_progress_seconds("frame=12") is None


# ---------------------------------------------------------------- torrent picker

SHOW_FILES = """
idx|path/length
===+===========================================================================
  1|./Show/ep1.mkv
   |1.2GiB (1,288,490,188)
---+---------------------------------------------------------------------------
  2|./Show/ep2.mkv
   |1.1GiB (1,181,116,006)
"""


def test_aria2_show_files_parsing_includes_sizes():
    assert tb.parse_aria2_show_files(SHOW_FILES) == [
        {"index": "1", "path": "./Show/ep1.mkv", "size": "1.2GiB"},
        {"index": "2", "path": "./Show/ep2.mkv", "size": "1.1GiB"},
    ]


def _picker_session(count):
    tb.torrent_select_sessions[1] = {
        "torrent_path": "/tmp/x.torrent",
        "files": [
            {"index": str(i), "path": f"./ep{i}.mkv", "size": "1GiB"} for i in range(1, count + 1)
        ],
        "selected": set(),
        "page": 0,
        "title": "Show",
    }


async def test_torrent_picker_pages_through_every_file():
    _picker_session(20)
    context = make_context()

    await tb.on_button(callback_update(context, "tpage:2"), context)

    labels = [b.text for b in _buttons(context.bot.calls[-1])]
    assert any("ep17.mkv" in label for label in labels)
    assert "3/3" in labels


async def test_torrent_picker_keeps_the_session_when_nothing_is_selected(monkeypatch):
    _picker_session(3)
    started = []

    async def fake_start(app, chat_id, source, user_id=None):
        started.append(source)
        job = {
            "id": 3,
            "name": "Show",
            "status": "starting",
            "chat_id": chat_id,
            "user_id": user_id,
            "source_type": "torrent",
        }
        tb.download_jobs[3] = job
        return job

    monkeypatch.setattr(tb, "start_aria2_download", fake_start)
    context = make_context()

    empty = callback_update(context, "tconfirm")
    await tb.on_button(empty, context)
    assert empty.callback_query.answers[0]["show_alert"] is True
    assert 1 in tb.torrent_select_sessions

    await tb.on_button(callback_update(context, "tselall"), context)
    await tb.on_button(callback_update(context, "tsel:2"), context)
    picker = callback_update(context, "tconfirm")
    await tb.on_button(picker, context)

    assert started == ["/tmp/x.torrent --select-file=1,3"]
    card = context.bot.calls[-1]
    assert (
        card.kwargs["message_id"] == picker.callback_query.message.message_id
    )  # picker became the card
    assert "#3 · Torrent · 2 of 3 files selected" in card.kwargs["text"]
    assert tb.download_jobs[3]["card"]["message_id"] == picker.callback_query.message.message_id


async def test_torrent_picker_expired_session_says_so():
    context = make_context()
    update = callback_update(context, "tsel:1")

    await tb.on_button(update, context)

    assert "expired" in update.callback_query.answers[0]["text"]


# ---------------------------------------------------------------- link prompts


async def test_each_video_prompt_downloads_its_own_link(monkeypatch, tmp_path):
    from app.services import user_settings

    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    started = []

    async def fake_ytdlp(
        app, chat_id, url, audio_only=False, user_id=None, max_height=None, **kwargs
    ):
        started.append((url, audio_only, max_height))
        job = {
            "id": len(started),
            "name": url,
            "status": "starting",
            "provider": "yt-dlp",
            "chat_id": chat_id,
            "user_id": user_id,
        }
        tb.download_jobs[job["id"]] = job
        return job

    monkeypatch.setattr(tb, "start_ytdlp_download", fake_ytdlp)
    monkeypatch.setattr(
        tb, "probe_video", lambda url: {"title": url, "duration": 75, "heights": [360, 720]}
    )
    monkeypatch.setattr(tb, "search_ui", None)
    context = make_context()

    await tb.on_text(text_update(context, "https://youtu.be/first"), context)
    await tb.on_text(text_update(context, "https://youtu.be/second"), context)
    await _drain_background()

    pickers = [
        c
        for c in context.bot.calls
        if c.method == "edit_text" and "Choose a quality" in c.kwargs["text"]
    ]
    assert len(pickers) == 2
    first = next(c for c in pickers if "first" in c.kwargs["text"])
    labels = [b.text for b in _buttons(first)]
    assert labels == ["⭐ Best (720p)", "480p", "🎵 MP3", "✖ Cancel"]  # 1080/720 caps hidden

    capped = next(b.callback_data for b in _buttons(first) if b.text == "480p")
    await tb.handle_link_request_callback(callback_update(context, capped), context)
    assert started == [("https://youtu.be/first", False, 480)]

    reused = callback_update(context, capped)
    await tb.handle_link_request_callback(reused, context)
    assert "already used" in reused.callback_query.answers[0]["text"]


async def test_video_default_setting_skips_the_picker(monkeypatch, tmp_path):
    from app.services import user_settings

    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    await user_settings.update_setting(1, "video_default", "mp3")
    started = []

    async def fake_ytdlp(
        app, chat_id, url, audio_only=False, user_id=None, max_height=None, **kwargs
    ):
        started.append((audio_only, max_height))
        job = {
            "id": 9,
            "name": "x",
            "status": "starting",
            "provider": "yt-dlp",
            "chat_id": chat_id,
            "user_id": user_id,
            "audio_only": audio_only,
        }
        tb.download_jobs[9] = job
        return job

    monkeypatch.setattr(tb, "start_ytdlp_download", fake_ytdlp)
    monkeypatch.setattr(tb, "search_ui", None)
    context = make_context()

    await tb.on_text(text_update(context, "https://youtu.be/x"), context)

    assert started == [(True, None)]
    assert "#9 · Video · MP3" in context.bot.texts()[-1]


async def test_old_style_prompt_buttons_explain_themselves():
    context = make_context()
    update = callback_update(context, "ytdlp_video")

    await tb.handle_stale_prompt_callback(update, context)

    assert "older version" in update.callback_query.answers[0]["text"]


async def test_duplicate_magnet_offers_download_anyway(monkeypatch):
    started = []

    async def fake_start(app, chat_id, source, user_id=None):
        started.append(source)
        return {"id": 4, "name": "x", "gid": "g", "source_type": "magnet"}

    async def no_status(*args, **kwargs):
        return None

    monkeypatch.setattr(tb, "is_duplicate_name", lambda name: True)
    monkeypatch.setattr(tb, "start_aria2_download", fake_start)
    monkeypatch.setattr(tb, "update_status_message", no_status)
    monkeypatch.setattr(tb, "search_ui", None)
    context = make_context()
    magnet = "magnet:?xt=urn:btih:abc&dn=Movie"

    await tb.on_text(text_update(context, magnet), context)

    prompt = context.bot.called("reply_text")[-1]
    assert "already in your downloads" in prompt.kwargs["text"]
    go = next(b.callback_data for b in _buttons(prompt) if "anyway" in b.text)
    await tb.handle_link_request_callback(callback_update(context, go), context)
    assert started == [magnet]


@pytest.mark.parametrize("text", ["https://example.com/help-center", "https://x.org/language"])
async def test_links_with_menu_words_are_not_treated_as_menu_buttons(text, monkeypatch):
    monkeypatch.setattr(tb, "search_ui", None)
    context = make_context()

    await tb.on_text(text_update(context, text), context)

    reply = context.bot.called("reply_text")[-1].kwargs["text"]
    assert "don't know how to download this link" in reply


# ---------------------------------------------------------------- /cancel


async def test_cancel_command_leaves_input_modes(monkeypatch):
    monkeypatch.setattr(tb, "search_ui", None)
    monkeypatch.setattr(tb, "zip_select_sessions", {1: {"waiting_for": "password"}})
    monkeypatch.setattr(tb, "pending_zip_name_sessions", {})
    context = make_context()

    await tb.cancel_input_cmd(text_update(context, "/cancel"), context)
    assert "archive password entry" in context.bot.texts()[-1]

    await tb.cancel_input_cmd(text_update(context, "/cancel"), context)
    assert context.bot.texts()[-1].startswith("Nothing to cancel")


# ---------------------------------------------------------------- bot-only mode


def test_no_pyrogram_login_is_reported_not_prompted(monkeypatch, tmp_path):
    monkeypatch.setattr(tb, "API_ID", 1)
    monkeypatch.setattr(tb, "API_HASH", "hash")
    monkeypatch.setattr(tb, "PYRO_SESSION_STRING", "")
    monkeypatch.setattr(tb, "BASE_DIR", tmp_path)
    monkeypatch.setattr(tb.sys, "stdin", None)

    assert "no Pyrogram login" in tb.pyrogram_unavailable_reason()
    monkeypatch.setattr(tb, "PYRO_SESSION_STRING", "abc")
    assert tb.pyrogram_unavailable_reason() is None
    monkeypatch.setattr(tb, "API_HASH", "")
    assert "API_ID/API_HASH" in tb.pyrogram_unavailable_reason()


async def test_bot_only_mode_sends_small_files_and_explains_big_ones(monkeypatch):
    (tb.DOWNLOAD_DIR / "small.txt").write_bytes(b"x" * 10)
    (tb.DOWNLOAD_DIR / "big.bin").write_bytes(b"x" * 100)
    monkeypatch.setattr(tb, "BOT_MAX_DOCUMENT_BYTES", 50)

    async def unavailable():
        raise tb.PyrogramUnavailable("no login")

    monkeypatch.setattr(tb, "get_pyrogram_client", unavailable)
    context = make_context()

    job = await tb.upload_files_via_pyrogram(context.application, 1, 2, ["small.txt"], title="t")
    assert job["status"] == "completed"
    assert context.bot.called("send_document")
    assert "Sent to: this chat" in context.bot.called("edit_message_text")[-1].kwargs["text"]

    with pytest.raises(RuntimeError, match="only files up to"):
        await tb.upload_files_via_pyrogram(context.application, 1, 3, ["big.bin"], title="t")


BOT_CHECK = (
    "ERROR: [youtube] abc123: Sign in to confirm you’re not a bot. Use --cookies-from-browser"
    " or --cookies for the authentication. See  https://github.com/yt-dlp/yt-dlp/wiki/FAQ"
)


def test_ytdlp_errors_are_explained():
    reason, fatal = tb.explain_ytdlp_error(BOT_CHECK)
    assert (
        fatal
        and "blocking downloads from this server" in reason
        and "Settings → 🍪 Cookies" in reason
    )

    reason, fatal = tb.explain_ytdlp_error(
        "ERROR: [generic] xyz: Requested format is not available. Use --list-formats"
    )
    assert not fatal and reason == "Requested format is not available."


async def test_blocked_video_shows_the_reason_instead_of_a_picker(monkeypatch):
    def blocked(url):
        raise RuntimeError(BOT_CHECK)

    monkeypatch.setattr(tb, "probe_video", blocked)
    monkeypatch.setattr(tb, "search_ui", None)
    context = make_context()

    await tb.on_text(text_update(context, "https://youtu.be/blocked"), context)
    await _drain_background()

    final = context.bot.texts()[-1]
    assert "Can't download this" in final and "Cookies" in final
    assert "Choose a quality" not in final
    assert tb.link_requests == {}


async def test_a_dropped_connection_on_tap_still_starts_the_download(monkeypatch):
    from telegram.error import NetworkError

    started = []

    async def fake_ytdlp(app, chat_id, url, **kwargs):
        started.append((url, kwargs.get("gif")))
        job = {
            "id": 1,
            "name": url,
            "status": "starting",
            "provider": "yt-dlp",
            "chat_id": chat_id,
            "user_id": 1,
        }
        tb.download_jobs[1] = job
        return job

    monkeypatch.setattr(tb, "start_ytdlp_download", fake_ytdlp)
    context = make_context()
    rid = tb.store_link_request("video", 1, url="https://x.com/a/status/1")
    update = callback_update(context, f"lp:{rid}:gif")

    async def disconnected(*args, **kwargs):
        raise NetworkError("Server disconnected without sending a response.")

    update.callback_query.answer = disconnected

    await tb.handle_link_request_callback(update, context)

    assert started == [("https://x.com/a/status/1", True)]


async def test_a_failed_start_keeps_the_prompt_for_another_tap(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(tb, "start_ytdlp_download", broken)
    context = make_context()
    rid = tb.store_link_request("video", 1, url="https://x.com/a/status/1")

    with pytest.raises(RuntimeError):
        await tb.handle_link_request_callback(callback_update(context, f"lp:{rid}:best"), context)

    assert rid in tb.link_requests


async def test_connect_failures_are_retried_once(monkeypatch):
    import httpx
    from telegram.error import NetworkError
    from telegram.request import HTTPXRequest

    calls = []

    async def flaky(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise NetworkError("httpx.ConnectError") from httpx.ConnectError("proxy")
        return 200, b"{}"

    monkeypatch.setattr(HTTPXRequest, "do_request", flaky)
    monkeypatch.setattr(tb.asyncio, "sleep", _no_sleep)
    assert await tb.RetryingRequest().do_request("https://x", "POST") == (200, b"{}")
    assert len(calls) == 2

    async def dropped(self, *args, **kwargs):
        raise NetworkError("disconnected") from httpx.RemoteProtocolError("gone")

    monkeypatch.setattr(HTTPXRequest, "do_request", dropped)
    with pytest.raises(NetworkError):  # may have been delivered: never resent
        await tb.RetryingRequest().do_request("https://x", "POST")


async def _no_sleep(*args, **kwargs):
    return None


async def test_buttons_survive_a_dropped_connection(monkeypatch):
    from telegram.error import NetworkError

    async def offline(*args, **kwargs):
        raise NetworkError("httpx.ConnectError")

    monkeypatch.setattr(tb, "show_screen", offline)
    context = make_context()
    update = callback_update(context, "nav:home")

    await tb.on_button(update, context)

    assert "Tap again" in update.callback_query.answers[0]["text"]


# ---------------------------------------------------------------- flood control


async def test_rate_limited_edit_does_not_sleep_and_pauses_that_chat(monkeypatch):
    """A RetryAfter used to sleep inside the edit, which stalled Pyrogram uploads
    (their progress callback edits the status message) to one part per back-off."""
    from telegram.error import RetryAfter

    monkeypatch.setattr(tb.flood_guard, "_blocked_until", {})
    context = make_context()
    calls = []

    async def flooded(**kwargs):
        calls.append(kwargs)
        raise RetryAfter(3600)

    monkeypatch.setattr(context.bot, "edit_message_text", flooded, raising=False)

    async def no_sleep(_):
        raise AssertionError("an edit must not sleep on a rate limit")

    monkeypatch.setattr(tb.asyncio, "sleep", no_sleep)
    app = context.application

    assert await tb._edit_dashboard_message(app, 7, 1, "a", None) is False
    assert await tb._edit_dashboard_message(app, 7, 1, "b", None) is False
    assert len(calls) == 1  # the second edit is skipped without calling Telegram
    # Other chats are unaffected.
    assert await tb._edit_dashboard_message(app, 8, 1, "c", None) is False
    assert len(calls) == 2


async def test_upload_queued_behind_another_says_it_is_waiting(monkeypatch):
    (tb.DOWNLOAD_DIR / "queued.txt").write_bytes(b"x")

    async def unavailable():
        raise tb.PyrogramUnavailable("no login")

    monkeypatch.setattr(tb, "get_pyrogram_client", unavailable)
    context = make_context()
    chain = tb._send_chain(1, "me")
    await chain.acquire()
    task = asyncio.create_task(
        tb.upload_files_via_pyrogram(context.application, 1, 2, ["queued.txt"], title="t")
    )
    await asyncio.sleep(0)
    assert "Waiting for the upload ahead" in context.bot.texts()[-1]
    chain.release()
    job = await task
    assert job["status"] == "completed"


def _request_data(**params):
    from telegram.request import RequestData
    from telegram.request._requestparameter import RequestParameter

    return RequestData([RequestParameter.from_input(k, v) for k, v in params.items()])


async def test_a_flood_blocked_chat_is_not_called_again_until_it_lifts(monkeypatch):
    """Telegram answered every send/edit in the chat with "retry after 52016"; the
    bot kept calling (dashboard, cards, previews, replies), which kept the block up."""
    from telegram.error import RetryAfter
    from telegram.request import HTTPXRequest

    calls = []

    async def telegram(self, url, method, request_data=None, *args, **kwargs):
        calls.append(url)
        if url.endswith("/sendMessage") and request_data.parameters["chat_id"] == 7:
            return 429, b'{"ok":false,"error_code":429,"parameters":{"retry_after":52016}}'
        return 200, b'{"ok":true,"result":true}'

    monkeypatch.setattr(HTTPXRequest, "do_request", telegram)
    request = tb.RetryingRequest()
    base = "https://api.telegram.org/botT/"

    code, _ = await request.do_request(base + "sendMessage", "POST", _request_data(chat_id=7, text="a"))
    assert code == 429
    assert 52000 < tb.flood_guard.blocked_for(7) <= 52016

    with pytest.raises(RetryAfter):
        await request.do_request(base + "editMessageText", "POST", _request_data(chat_id=7, message_id=1, text="b"))
    assert len(calls) == 1  # refused locally

    # Other chats, chat actions and calls without a chat still go out.
    await request.do_request(base + "sendMessage", "POST", _request_data(chat_id=8, text="c"))
    await request.do_request(base + "sendChatAction", "POST", _request_data(chat_id=7, action="typing"))
    await request.do_request(base + "answerCallbackQuery", "POST", _request_data(callback_query_id="q"))
    assert len(calls) == 4

    tb.flood_guard._blocked_until[7] = 0  # lifted
    await request.do_request(base + "editMessageText", "POST", _request_data(chat_id=7, message_id=1, text="d"))
    assert len(calls) == 5


async def test_rate_limited_safe_edit_does_not_reply_instead():
    from telegram.error import RetryAfter

    context = make_context()
    message = callback_update(context, "x").callback_query.message

    async def flooded(*args, **kwargs):
        raise RetryAfter(60)

    message.edit_text = flooded
    await tb.safe_edit_message(message, "hello")
    assert not context.bot.called("reply_text")


async def test_upload_tap_in_a_flood_blocked_chat_explains_and_still_uploads(monkeypatch):
    """The card used to sit on "Preparing upload..." with no hint, and a job card's
    Upload button did not upload at all (its "Preparing" message could not be sent)."""
    from telegram.error import RetryAfter

    (tb.DOWNLOAD_DIR / "clip.mp4").write_bytes(b"x")
    tb.download_jobs[3] = {
        "id": 3, "name": "clip.mp4", "status": "completed", "chat_id": 1, "files": ["clip.mp4"],
        "filepath": str(tb.DOWNLOAD_DIR / "clip.mp4"),
    }
    monkeypatch.setattr(tb, "job_files", lambda job: ["clip.mp4"])
    tb.flood_guard.note(1, 3600)
    context = make_context()

    async def blocked_send(**kwargs):
        raise RetryAfter(3600)

    monkeypatch.setattr(context.bot, "send_message", blocked_send, raising=False)
    started = []

    async def fake_upload(app, chat_id, message_id, files, **kwargs):
        started.append((chat_id, message_id, files))

    monkeypatch.setattr(tb, "upload_files_via_pyrogram", fake_upload)
    update = callback_update(context, "job:up:3")

    await tb.handle_job_callback(update, context)
    await _drain_background()

    answer = update.callback_query.answers[0]
    assert answer["show_alert"] and "UTC" in answer["text"] and len(answer["text"]) <= 200
    assert started == [(1, None, ["clip.mp4"])]


async def test_upload_result_is_shown_once_flood_control_lifts(monkeypatch):
    context = make_context()
    tb.flood_guard.note(1, 3600)
    tb.flood_guard._last_probe[1] = time.time()  # a send there was just refused too
    waited = []

    async def wait(seconds):
        waited.append(seconds)
        tb.flood_guard._blocked_until.clear()  # time passes; the block lifts

    monkeypatch.setattr(tb.asyncio, "sleep", wait)
    await tb._show_upload_result(context.application, 1, 42, "Upload complete")
    assert not context.bot.calls  # nothing sent into the blocked chat
    await _drain_background()

    assert waited and waited[0] == tb.HELD_RECHECK
    edit = context.bot.called("edit_message_text")[0]
    assert edit.kwargs["message_id"] == 42 and edit.kwargs["text"] == "Upload complete"

    # With no progress message (it could not be sent) the summary is posted fresh.
    await tb._show_upload_result(context.application, 1, None, "Upload complete")
    assert context.bot.called("send_message")[0].kwargs["text"] == "Upload complete"
