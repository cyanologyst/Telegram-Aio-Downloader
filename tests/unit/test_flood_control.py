"""Regression tests for uploads and replies while Telegram's flood control blocks the bot chat.

The bot chat got a 14-hour 429 ("retry after 51308"). Every reply, card and
summary there was refused, so the bot looked dead: typed messages got nothing
back, upload results vanished on the next restart, a failed upload said
nothing, auto-uploads never started, and previews were skipped.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from pyrogram.errors import FloodWait
from telegram.error import RetryAfter

import app.bot.telegram_bot as tb
from app.bot import flood_guard
from tests.fakes import callback_update, make_context, text_update


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "upload_jobs", {})
    monkeypatch.setattr(tb, "status_messages", {})
    monkeypatch.setattr(flood_guard, "_blocked_until", {})
    monkeypatch.setattr(flood_guard, "_held", {})
    monkeypatch.setattr(flood_guard, "_announced", {})
    monkeypatch.setattr(flood_guard, "_sends_open", set())
    monkeypatch.setattr(flood_guard, "_last_probe", {})
    monkeypatch.setattr(flood_guard, "_state_path", None)
    monkeypatch.setattr(tb, "_held_flushers", {})
    monkeypatch.setattr(tb, "HELD_SPACING", 0)
    return tmp_path


async def _drain_background():
    while tb.background_tasks:
        await asyncio.gather(*list(tb.background_tasks), return_exceptions=True)
        await asyncio.sleep(0)


def _no_user_account(monkeypatch):
    async def unavailable():
        raise tb.PyrogramUnavailable("no login")

    monkeypatch.setattr(tb, "get_pyrogram_client", unavailable)


def _sends_refused(chat_id, seconds=3600):
    """Block ``chat_id`` and record that a send probe there was just refused too."""
    flood_guard.note(chat_id, seconds)
    flood_guard._last_probe[chat_id] = time.time()


def _time_passes(monkeypatch):
    """asyncio.sleep returns at once and lifts every block, as if the hours went by."""
    waited = []
    real_sleep = asyncio.sleep

    async def sleep(seconds):
        waited.append(seconds)
        if seconds >= tb.HELD_RECHECK:
            flood_guard._blocked_until.clear()
        await real_sleep(0)

    monkeypatch.setattr(tb.asyncio, "sleep", sleep)
    return waited


# ---------------------------------------------------------------- flood_guard state


def test_block_and_held_results_survive_a_restart(tmp_path):
    """The bot was restarted three times during one block; each restart forgot it
    (and called Telegram again) and dropped every result it was holding."""
    state = tmp_path / "flood.json"
    flood_guard.configure(state)
    flood_guard.note(7, 3600)
    flood_guard.hold(7, {"text": "Upload complete", "message_id": 5, "buttons": "upload_done"})

    flood_guard._blocked_until.clear()
    flood_guard._held.clear()
    flood_guard.configure(state)  # the next run

    assert 3590 < flood_guard.blocked_for(7) <= 3600
    assert flood_guard.held(7) == [
        {"text": "Upload complete", "message_id": 5, "buttons": "upload_done"}
    ]


def test_expired_blocks_are_not_restored(tmp_path):
    state = tmp_path / "flood.json"
    state.write_text(json.dumps({"blocked_until": {"7": time.time() - 10}, "held": {}}))
    flood_guard.configure(state)
    assert flood_guard.blocked_for(7) == 0


def test_unreadable_state_file_does_not_stop_the_bot(tmp_path):
    state = tmp_path / "flood.json"
    state.write_text("{not json")
    flood_guard.configure(state)
    assert flood_guard.blocked_for(7) == 0


def test_held_messages_are_capped_per_chat():
    for n in range(flood_guard.MAX_HELD_PER_CHAT + 10):
        flood_guard.hold(7, {"text": str(n)})
    items = flood_guard.held(7)
    assert len(items) == flood_guard.MAX_HELD_PER_CHAT
    assert items[-1]["text"] == str(flood_guard.MAX_HELD_PER_CHAT + 9)  # newest kept


def test_announcement_is_due_once_per_block():
    assert not flood_guard.take_announcement(7)  # not blocked
    flood_guard.note(7, 3600)
    assert flood_guard.take_announcement(7)
    assert not flood_guard.take_announcement(7)
    flood_guard._blocked_until[7] = 0  # lifted
    flood_guard.blocked_for(7)
    flood_guard.note(7, 3600)  # a new block
    assert flood_guard.take_announcement(7)


# ---------------------------------------------------------------- held results


async def test_results_of_many_uploads_are_posted_one_by_one_after_the_block(monkeypatch):
    """Each held result used to get its own task sleeping out the block; they all
    woke together and fired a burst of edits, the kind that earns a new block."""
    context = make_context()
    _sends_refused(1)
    _time_passes(monkeypatch)

    for n in range(5):
        await tb._show_upload_result(context.application, 1, 100 + n, f"Upload complete {n}")
    assert not context.bot.calls
    assert len(tb._held_flushers) == 1  # one flusher for the chat, not one per result

    await _drain_background()
    edits = context.bot.called("edit_message_text")
    assert [c.kwargs["text"] for c in edits] == [f"Upload complete {n}" for n in range(5)]
    assert [c.kwargs["message_id"] for c in edits] == [100, 101, 102, 103, 104]
    assert flood_guard.held(1) == []


async def test_a_new_block_while_posting_held_results_waits_again(monkeypatch):
    context = make_context()
    flood_guard.hold(1, {"text": "first", "message_id": None, "buttons": None})
    flood_guard.hold(1, {"text": "second", "message_id": None, "buttons": None})
    _time_passes(monkeypatch)
    sent = []

    async def send_message(**kwargs):
        if not sent:
            sent.append("refused")
            raise RetryAfter(600)
        sent.append(kwargs["text"])
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(context.bot, "send_message", send_message, raising=False)
    tb.schedule_held_flush(context.application, 1)
    await _drain_background()

    assert sent == ["refused", "first", "second"]
    assert flood_guard.held(1) == []


async def test_a_held_message_telegram_never_takes_is_given_up(monkeypatch):
    context = make_context()
    flood_guard.hold(1, {"text": "doomed", "message_id": None, "buttons": None})
    _time_passes(monkeypatch)
    attempts = []

    async def send_message(**kwargs):
        attempts.append(1)
        raise RetryAfter(600)

    monkeypatch.setattr(context.bot, "send_message", send_message, raising=False)
    tb.schedule_held_flush(context.application, 1)
    await asyncio.wait_for(_drain_background(), 5)

    assert len(attempts) == tb.HELD_MAX_ATTEMPTS
    assert flood_guard.held(1) == []


async def test_failed_upload_in_a_blocked_chat_is_reported_when_it_lifts(monkeypatch):
    """The failure notice was an edit that flood control refused: nothing ever said it failed."""
    context = make_context()
    _sends_refused(1)
    _time_passes(monkeypatch)

    report = tb.message_error_reporter(context.application, 1, 42, "Upload")
    await report(RuntimeError("disk on fire"))
    assert not context.bot.calls
    await _drain_background()

    edit = context.bot.called("edit_message_text")[0]
    assert edit.kwargs["message_id"] == 42 and "disk on fire" in edit.kwargs["text"]


async def test_failed_upload_without_a_progress_message_is_still_reported():
    """With no message (its "Preparing" message hit flood control) the edit was skipped."""
    context = make_context()
    report = tb.message_error_reporter(context.application, 1, None, "Upload")
    await report(RuntimeError("disk on fire"))
    assert "disk on fire" in context.bot.called("send_message")[0].kwargs["text"]


async def test_upload_in_a_blocked_chat_runs_and_its_result_is_held(monkeypatch):
    (tb.DOWNLOAD_DIR / "a.txt").write_bytes(b"x")
    _no_user_account(monkeypatch)
    _sends_refused(1)
    context = make_context()

    job = await tb.upload_files_via_pyrogram(context.application, 1, None, ["a.txt"], title="t")

    assert job["status"] == "completed"
    assert context.bot.called("send_document")  # the file went out
    assert [i["text"] for i in flood_guard.held(1)][0].startswith(f"{tb.ICON_OK} Upload complete")


# ---------------------------------------------------------------- final cards and auto-upload


async def test_finished_job_in_a_blocked_chat_still_auto_uploads(monkeypatch):
    """The final card could not be sent, and finish_job_card returned before
    starting the auto-upload, so finished downloads were never uploaded."""
    (tb.DOWNLOAD_DIR / "clip.mp4").write_bytes(b"x")
    job = {
        "id": 9,
        "name": "clip.mp4",
        "status": "completed",
        "chat_id": 1,
        "user_id": 1,
        "provider": "aria2",
        "files": ["clip.mp4"],
    }
    tb.download_jobs[9] = job
    monkeypatch.setattr(tb, "get_user_settings", lambda uid: {"auto_upload_after_download": True})
    monkeypatch.setattr(tb, "job_outputs", lambda j: ["clip.mp4"])
    monkeypatch.setattr(tb, "filter_auto_upload_files", lambda j, preset: (["clip.mp4"], 0))
    uploads = []

    async def start_upload(app, j, files, *, auto=False):
        uploads.append((j["id"], files, auto))
        return True

    monkeypatch.setattr(tb, "start_job_upload", start_upload)
    flood_guard.note(1, 3600)
    context = make_context()
    real_send = context.bot.send_message

    async def send_message(**kwargs):
        if flood_guard.blocked_for(kwargs["chat_id"]):
            raise RetryAfter(int(flood_guard.blocked_for(kwargs["chat_id"])))
        return await real_send(**kwargs)

    monkeypatch.setattr(context.bot, "send_message", send_message, raising=False)
    _time_passes(monkeypatch)

    await tb.finish_job_card(context.application, job)
    assert uploads == [(9, ["clip.mp4"], True)]
    assert flood_guard.held(1) == [{"job_id": 9}]

    await _drain_background()  # the block lifts; the card is posted, no second upload
    assert job["card_final"] == "completed"
    assert context.bot.called("send_message")
    assert uploads == [(9, ["clip.mp4"], True)]
    assert flood_guard.held(1) == []


# ---------------------------------------------------------------- previews


async def test_preview_still_reaches_saved_messages_when_the_bot_chat_is_blocked(
    monkeypatch, tmp_path
):
    """The "Making GIF previews" note was sent first; its 429 aborted the preview,
    and with auto-delete on the video was then deleted without one."""
    source = tmp_path / "v.mp4"
    source.write_bytes(b"x")
    merged = tmp_path / "Preview - v.mp4"
    merged.write_bytes(b"x")
    monkeypatch.setattr(tb, "make_gif_previews", lambda src, tmp: [(merged, 1.0)])
    monkeypatch.setattr(
        tb, "merge_gif_previews", lambda clips, src, tmp: (merged, SimpleNamespace())
    )
    videos = []

    class Client:
        async def send_video(self, **kwargs):
            videos.append(kwargs["chat_id"])

    async def client():
        return Client()

    monkeypatch.setattr(tb, "get_pyrogram_client", client)
    context = make_context()

    async def blocked(**kwargs):
        raise RetryAfter(3600)

    monkeypatch.setattr(context.bot, "send_message", blocked, raising=False)

    sent = await tb.send_gif_previews(
        context.application, 1, source, status_chat_id=1, fallback_chat_id=1, target="me"
    )
    assert sent == 1 and videos == ["me"]


# ---------------------------------------------------------------- Pyrogram


async def test_a_long_floodwait_fails_the_send_instead_of_sleeping_for_hours(monkeypatch):
    """It slept e.value seconds while holding the chat's send chain and the preview
    lock, so every other upload waited behind it."""
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(tb.asyncio, "sleep", sleep)

    async def send():
        raise FloodWait(value=50000)

    with pytest.raises(FloodWait):
        await tb.send_with_flood_wait_handling(send)
    assert slept == []


async def test_a_short_floodwait_is_waited_out(monkeypatch):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(tb.asyncio, "sleep", sleep)
    calls = []

    async def send():
        calls.append(1)
        if len(calls) == 1:
            raise FloodWait(value=3)
        return "ok"

    assert await tb.send_with_flood_wait_handling(send) == "ok"
    assert slept == [3]


async def test_concurrent_callers_share_one_user_client_and_a_failed_start_is_retried(monkeypatch):
    started = []
    fail = [True]

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def start(self):
            await asyncio.sleep(0)
            if fail[0]:
                fail[0] = False
                raise ConnectionError("no network")
            started.append(self)

        async def get_me(self):
            return SimpleNamespace(is_bot=False, id=1)

        async def stop(self):
            pass

    monkeypatch.setattr(tb, "Client", Client)
    monkeypatch.setattr(tb, "pyro_client", None)
    monkeypatch.setattr(tb, "pyrogram_unavailable_reason", lambda: None)

    with pytest.raises(ConnectionError):
        await tb.get_pyrogram_client()
    assert tb.pyro_client is None  # an unstarted client is not handed out later

    clients = await asyncio.gather(*(tb.get_pyrogram_client() for _ in range(4)))
    assert len(started) == 1 and all(c is started[0] for c in clients)


# ---------------------------------------------------------------- replies


async def test_typed_message_in_a_blocked_chat_is_handled_quietly_and_explained_once(
    monkeypatch, caplog
):
    """on_text caught the refused reply, then replied again from its except block;
    that raised too, and every message logged two tracebacks and got no answer.
    The user had no way to learn why the bot had gone silent."""
    notices = []

    class Client:
        me = SimpleNamespace(id=1)

        async def send_message(self, chat_id, text):
            notices.append((chat_id, text))

    async def client():
        return Client()

    monkeypatch.setattr(tb, "get_pyrogram_client", client)
    flood_guard.note(1, 3600)
    context = make_context()
    for _ in range(3):
        update = text_update(context, "/files", user_id=1)
        context.error = RetryAfter(3600)
        await tb.error_handler(update, context)
    await _drain_background()

    assert len(notices) == 1 and notices[0][0] == "me" and "UTC" in notices[0][1]
    assert not any(r.exc_info for r in caplog.records)


async def test_flood_notice_goes_only_to_the_blocked_users_own_saved_messages(monkeypatch):
    notices = []

    class Client:
        me = SimpleNamespace(id=99)  # the user account is someone else

        async def send_message(self, chat_id, text):
            notices.append(chat_id)

    async def client():
        return Client()

    monkeypatch.setattr(tb, "get_pyrogram_client", client)
    flood_guard.note(1, 3600)
    await tb.announce_flood_block(1)
    assert notices == []


async def test_button_in_a_blocked_chat_explains_the_block_in_a_popup(monkeypatch):
    _no_user_account(monkeypatch)
    context = make_context()
    update = callback_update(context, "nav:home")

    async def blocked(*args, **kwargs):
        raise RetryAfter(3600)

    update.callback_query.message.edit_text = blocked
    update.callback_query.message.reply_text = blocked

    await tb.on_button(update, context)
    await _drain_background()

    answer = update.callback_query.answers[0]
    assert answer["show_alert"] and "UTC" in answer["text"] and len(answer["text"]) <= 200
    assert "Something went wrong" not in answer["text"]


# ---------------------------------------------------------------- mini-app


async def test_mini_app_upload_returns_at_once_and_runs_in_the_background(monkeypatch):
    """It awaited the whole upload while the web request waited 30 s, so a long
    upload came back as an error and invited a second, duplicate upload."""
    gate = asyncio.Event()
    uploads = []

    async def upload(app, chat_id, message_id, rel_path, file_list=None, user_id=None):
        uploads.append(file_list)
        await gate.wait()

    monkeypatch.setattr(tb, "send_folder_files_via_pyrogram", upload)
    context = make_context()

    result = await asyncio.wait_for(
        tb.upload_mini_app_selection(context.application, 1, ["a.mp4"], 1), 1
    )
    assert "Queued" in result
    await asyncio.sleep(0)
    assert uploads == [["a.mp4"]]
    gate.set()
    await _drain_background()


# ---------------------------------------------------------------- sends during a block


def _request_data(**params):
    from telegram.request import RequestData
    from telegram.request._requestparameter import RequestParameter

    return RequestData([RequestParameter.from_input(k, v) for k, v in params.items()])


_API = "https://api.telegram.org/botT/"
_429 = b'{"ok":false,"error_code":429,"parameters":{"retry_after":46481}}'


def _fake_telegram(monkeypatch, answer):
    """Route Bot API calls to ``answer(method)`` -> HTTP code; returns the methods called."""
    from telegram.request import HTTPXRequest

    calls = []

    async def telegram(self, url, method, request_data=None, *args, **kwargs):
        name = url.rsplit("/", 1)[-1]
        calls.append(name)
        code = answer(name)
        return code, (_429 if code == 429 else b'{"ok":true,"result":true}')

    monkeypatch.setattr(HTTPXRequest, "do_request", telegram)
    return calls


async def test_commands_in_a_blocked_chat_still_reach_telegram_and_get_answered(monkeypatch):
    """Telegram took the Files list and "Preparing upload..." in the middle of a
    14-hour block (only edits were refused), but once the guard had seen one 429
    it refused every send locally: Files, Search and Status got no reply for hours."""
    calls = _fake_telegram(monkeypatch, lambda name: 429 if name.startswith("edit") else 200)
    request = tb.RetryingRequest()
    flood_guard.note(7, 46481)

    for text in ("📁 Files", "🔍 Search", "📊 Status"):
        code, _ = await request.do_request(
            _API + "sendMessage", "POST", _request_data(chat_id=7, text=text)
        )
        assert code == 200
    assert calls == ["sendMessage"] * 3
    assert flood_guard.sends_open(7)

    # Edits stay refused locally for the rest of the block.
    with pytest.raises(RetryAfter):
        await request.do_request(
            _API + "editMessageText", "POST", _request_data(chat_id=7, message_id=1, text="x")
        )
    assert calls == ["sendMessage"] * 3
    assert flood_guard.blocked_for(7) > 46000


async def test_a_refused_send_is_probed_again_at_most_once_a_minute(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(flood_guard.time, "time", lambda: now[0])
    refusing = [True]
    calls = _fake_telegram(monkeypatch, lambda name: 429 if refusing[0] else 200)
    request = tb.RetryingRequest()
    flood_guard.note(7, 46481)

    code, _ = await request.do_request(
        _API + "sendMessage", "POST", _request_data(chat_id=7, text="a")
    )
    assert code == 429 and not flood_guard.sends_open(7)
    now[0] += 10
    with pytest.raises(RetryAfter):  # within the minute: refused locally
        await request.do_request(_API + "sendMessage", "POST", _request_data(chat_id=7, text="b"))
    assert len(calls) == 1

    refusing[0] = False
    now[0] += flood_guard.SEND_PROBE_INTERVAL
    code, _ = await request.do_request(
        _API + "sendMessage", "POST", _request_data(chat_id=7, text="c")
    )
    assert code == 200 and flood_guard.sends_open(7) and len(calls) == 2

    # A later refusal closes sends again.
    refusing[0] = True
    await request.do_request(_API + "sendMessage", "POST", _request_data(chat_id=7, text="d"))
    assert not flood_guard.sends_open(7)


async def test_upload_result_replaces_a_stuck_preparing_message_once_sends_work(monkeypatch):
    """The upload finished but "Preparing upload..." could not be edited, and the
    result was held for the whole 14 hours. Once Telegram takes new messages, the
    result is posted as a new one (the edit is still refused)."""
    context = make_context()
    flood_guard.note(1, 3600)
    flood_guard.hold(1, {"text": "Upload complete", "message_id": 55, "buttons": "upload_done"})
    flood_guard.send_result(1, True)  # a reply in the chat just went through

    tb.schedule_held_flush(context.application, 1)
    await asyncio.wait_for(_drain_background(), 5)

    assert not context.bot.called("edit_message_text")
    assert [c.kwargs["text"] for c in context.bot.called("send_message")] == ["Upload complete"]
    assert flood_guard.held(1) == []
    assert flood_guard.blocked_for(1) > 3000  # the block itself is still remembered


async def test_held_results_wait_while_sends_are_still_refused(monkeypatch):
    context = make_context()
    flood_guard.note(1, 3600)
    flood_guard.hold(1, {"text": "Upload complete", "message_id": 55, "buttons": "upload_done"})
    flood_guard.hold(1, {"job_id": 3})
    waited = _time_passes(monkeypatch)

    tb.schedule_held_flush(context.application, 1)
    await asyncio.wait_for(_drain_background(), 5)

    assert waited[0] == tb.HELD_RECHECK  # rechecked, not slept for the whole block
    assert [c.kwargs["text"] for c in context.bot.called("edit_message_text")] == [
        "Upload complete"
    ]


async def test_upload_result_is_sent_new_when_its_progress_message_cannot_be_edited(monkeypatch):
    context = make_context()
    flood_guard.note(1, 3600)  # no send tried yet, so one may go out as a probe

    await tb._show_upload_result(context.application, 1, 55, "Upload complete")

    assert not context.bot.called("edit_message_text")
    assert [c.kwargs["text"] for c in context.bot.called("send_message")] == ["Upload complete"]
    assert flood_guard.held(1) == []
