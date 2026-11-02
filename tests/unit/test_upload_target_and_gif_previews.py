"""Upload target (channel/group instead of Saved Messages) and GIF previews."""

import asyncio
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from telegram.error import Forbidden

import app.bot.telegram_bot as tb
from app.bot.views import jobs as job_views
from app.bot.views import settings as settings_views
from app.services import gif_previews, user_settings
from tests.fakes import callback_update, make_context, text_update

REAL_MERGE = tb.merge_gif_previews


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "Download"
    root.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", root)
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "upload_jobs", {})
    return root


async def _drain_background():
    while tb.background_tasks:
        await asyncio.gather(*list(tb.background_tasks), return_exceptions=True)
        await asyncio.sleep(0)


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


# ---------------------------------------------------------------- settings


@pytest.mark.parametrize(
    "text, ref",
    [
        ("-1001234567890", -1001234567890),
        ("@my_channel", "@my_channel"),
        ("my_channel", "@my_channel"),
        ("https://t.me/my_channel", "@my_channel"),
        ("t.me/my_channel/", "@my_channel"),
        ("hello world", None),
        ("@ab", None),
        ("https://t.me/+AbCdEf", None),
        ("", None),
    ],
)
def test_chat_references_are_parsed(text, ref):
    assert user_settings.parse_chat_ref(text) == ref


async def test_target_defaults_to_saved_messages_and_round_trips():
    s = user_settings.get_user_settings(1)
    assert user_settings.upload_target(s) == "me"
    assert user_settings.upload_target_label(s) == "Saved Messages"

    assert await user_settings.update_setting(1, "target_chat_id", -100123)
    assert await user_settings.update_setting(1, "target_chat_title", "My channel")
    s = user_settings.get_user_settings(1)
    assert user_settings.upload_target(s) == -100123
    assert user_settings.upload_target_label(s) == "My channel"

    await user_settings.update_setting(1, "target_chat_id", None)
    assert user_settings.upload_target(user_settings.get_user_settings(1)) == "me"


def test_settings_screen_links_to_the_target_screen():
    _, markup = settings_views.settings_screen(user_settings.get_user_settings(1))
    assert "nav:target" in _callbacks(markup)
    _, markup = settings_views.upload_target_screen({"target_chat_id": None})
    assert _callbacks(markup) == ["tg:set", "nav:settings", "nav:home"]
    text, markup = settings_views.upload_target_screen(
        {"target_chat_id": -100123, "target_chat_title": "Chan"}
    )
    assert "Chan" in text and "tg:reset" in _callbacks(markup)


async def test_typing_a_target_validates_and_saves_it(monkeypatch):
    checked = []

    async def resolve(bot, ref):
        checked.append(ref)
        return -100555, "Movies"

    monkeypatch.setattr(tb, "resolve_upload_target", resolve)
    context = make_context()
    await tb.on_button(callback_update(context, "tg:set"), context)
    assert tb.target_waiting(context)

    await tb.on_text(text_update(context, "https://t.me/movies_chan"), context)

    assert checked == ["@movies_chan"]
    assert not tb.target_waiting(context)
    s = user_settings.get_user_settings(1)
    assert s["target_chat_id"] == -100555 and s["target_chat_title"] == "Movies"

    await tb.on_button(callback_update(context, "tg:reset"), context)
    assert user_settings.get_user_settings(1)["target_chat_id"] is None


async def test_a_rejected_target_keeps_waiting_and_says_why(monkeypatch):
    async def resolve(bot, ref):
        raise ValueError("Only admins can post in a channel; make your account an admin.")

    monkeypatch.setattr(tb, "resolve_upload_target", resolve)
    context = make_context()
    context.user_data["target_wait"] = tb.time.time()

    await tb.on_text(text_update(context, "@somechan"), context)

    assert tb.target_waiting(context)
    assert any("Only admins" in t for t in context.bot.texts())
    assert user_settings.get_user_settings(1)["target_chat_id"] is None


async def test_account_check_requires_post_rights_in_channels():
    enums = tb.pyro_enums

    def client(status, can_post=False):
        async def get_chat(ref):
            return SimpleNamespace(id=-1009, type=enums.ChatType.CHANNEL, title="C", username=None)

        async def get_chat_member(chat_id, user):
            return SimpleNamespace(
                status=status, privileges=SimpleNamespace(can_post_messages=can_post)
            )

        return SimpleNamespace(get_chat=get_chat, get_chat_member=get_chat_member)

    assert await tb._check_target_with_account(
        client(enums.ChatMemberStatus.ADMINISTRATOR, True), "@c"
    ) == (-1009, "C")
    assert await tb._check_target_with_account(client(enums.ChatMemberStatus.OWNER), "@c")
    for status in (enums.ChatMemberStatus.MEMBER, enums.ChatMemberStatus.ADMINISTRATOR):
        with pytest.raises(ValueError):
            await tb._check_target_with_account(client(status), "@c")


async def test_uploads_go_to_the_target_chat(monkeypatch):
    (tb.DOWNLOAD_DIR / "a.bin").write_bytes(b"x" * 10)
    await user_settings.update_setting(5, "target_chat_id", -100777)
    await user_settings.update_setting(5, "target_chat_title", "Archive")
    targets = []

    async def fake_send(rel_path, progress_callback=None, chat_id="me", **_):
        targets.append(chat_id)

    async def logged_in():
        return object()

    monkeypatch.setattr(tb, "pyrogram_send_file", fake_send)
    monkeypatch.setattr(tb, "get_pyrogram_client", logged_in)
    context = make_context()

    job = await tb.upload_files_via_pyrogram(
        context.application, 1, 2, ["a.bin"], title="t", user_id=5
    )

    assert job["status"] == "completed" and targets == [-100777]
    assert "Sent to: Archive" in context.bot.called("edit_message_text")[-1].kwargs["text"]


async def test_bot_only_mode_sends_to_the_target_chat(monkeypatch):
    (tb.DOWNLOAD_DIR / "small.txt").write_bytes(b"x" * 10)
    await user_settings.update_setting(5, "target_chat_id", -100777)

    async def unavailable():
        raise tb.PyrogramUnavailable("no login")

    monkeypatch.setattr(tb, "get_pyrogram_client", unavailable)
    context = make_context()

    await tb.upload_files_via_pyrogram(
        context.application, 1, 2, ["small.txt"], title="t", user_id=5
    )

    assert context.bot.called("send_document")[-1].kwargs["chat_id"] == -100777


# ---------------------------------------------------------------- GIF previews


def test_preview_plan_spreads_ten_clips_over_a_long_video():
    plan = gif_previews.preview_plan(600)
    assert len(plan) == 10
    assert all(length == 6 for _, length in plan)
    assert plan[0][0] == 27 and plan[-1][0] == 567
    assert all(0 <= start <= 594 for start, _ in plan)


@pytest.mark.parametrize("duration, count", [(60, 10), (59, 9), (30, 5), (7, 1), (6, 1)])
def test_short_videos_get_fewer_clips(duration, count):
    plan = gif_previews.preview_plan(duration)
    assert len(plan) == count
    assert all(s >= 0 and s + length <= duration + 0.01 for s, length in plan)


def test_tiny_or_unknown_videos():
    assert gif_previews.preview_plan(2.5) == [(0.0, 2.5)]
    assert gif_previews.preview_plan(None) == []
    assert gif_previews.preview_plan(0) == []


def test_timestamps():
    assert gif_previews.timestamp(754) == "12:34"
    assert gif_previews.timestamp(3725) == "1:02:05"


def test_preview_button_on_video_job_cards(isolated, monkeypatch):
    (isolated / "clip.mp4").write_bytes(b"x")
    (isolated / "notes.txt").write_bytes(b"x")
    outputs = ["clip.mp4"]
    monkeypatch.setattr(tb, "job_outputs", lambda job: outputs)
    job = {"id": 3, "name": "clip", "status": "completed", "provider": "yt-dlp", "chat_id": 1}

    assert "job:gp:3" in _callbacks(tb.render_job_card(job)[1])
    outputs[:] = ["notes.txt"]
    assert "job:gp:3" not in _callbacks(tb.render_job_card(job)[1])
    _, markup = job_views.job_card({"id": 4, "status": "completed"}, can_upload=True)
    assert "job:gp:4" not in _callbacks(markup)


async def test_missing_ffmpeg_reports_instead_of_failing(isolated, monkeypatch):
    (isolated / "v.mp4").write_bytes(b"x")
    monkeypatch.setattr(tb, "FFMPEG_BIN", "/nonexistent/ffmpeg")
    context = make_context()

    await tb.on_button(callback_update(context, f"fb:gifprev:0:{tb.encode_path('v.mp4')}"), context)
    await _drain_background()

    assert any("ffmpeg/ffprobe are not installed" in t for t in context.bot.texts())
    assert not context.bot.called("send_media_group")


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg"
)
async def test_previews_of_a_real_short_video(isolated, monkeypatch):
    video = isolated / "test.mp4"
    subprocess.run(  # noqa: ASYNC221
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=25",
            "-t",
            "20",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )
    monkeypatch.setattr(tb, "FFMPEG_BIN", "ffmpeg")
    monkeypatch.setattr(tb, "FFPROBE_BIN", "ffprobe")
    monkeypatch.setattr(tb, "merge_gif_previews", REAL_MERGE)
    context = make_context()

    await tb.send_gif_previews(context.application, 1, video)

    assert not context.bot.called("send_media_group") and not context.bot.called("send_animation")
    (sent,) = context.bot.called("send_video")
    assert sent.kwargs["chat_id"] == 1
    assert "caption" not in sent.kwargs
    assert sent.kwargs["width"] == 480 and sent.kwargs["height"] == 270
    assert 17 <= sent.kwargs["duration"] <= 19  # three 6 s clips in one file
    assert not list(isolated.glob("preview_*")) and not list(isolated.glob("Preview - *"))


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="needs ffmpeg"
)
def test_merge_keeps_the_clips_in_order(tmp_path):
    clips = []
    for i, colour in enumerate(("red", "green", "blue")):
        clip = tmp_path / f"clip {i}'s.mp4"  # spaces and quotes survive the concat list
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colour}:size=64x64:rate=12",
                "-t",
                "1",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            ],
            check=True,
        )
        clips.append(clip)
    target = tmp_path / gif_previews.merged_name("My Movie.mkv")

    gif_previews.merge_clips(clips, target)

    assert target.name == "Preview - My Movie.mp4"
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".txt"] == []  # list file cleaned up

    def colour_at(second):
        rgb = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(second),
                "-i",
                str(target),
                "-frames:v",
                "1",
                "-vf",
                "scale=1:1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            capture_output=True,
            check=True,
        ).stdout
        return max(range(3), key=lambda i: rgb[i])

    assert [colour_at(t) for t in (0.5, 1.5, 2.5)] == [0, 1, 2]  # red, green, blue


def test_merge_failure_raises_and_leaves_nothing(tmp_path):
    clip = tmp_path / "bad.mp4"
    clip.write_bytes(b"not a video")
    target = tmp_path / "Preview - x.mp4"

    with pytest.raises(RuntimeError):
        gif_previews.merge_clips([clip], target, ffmpeg=shutil.which("ffmpeg") or "false")

    assert not target.exists() and not list(tmp_path.glob("*.txt"))


def test_merged_name():
    assert gif_previews.merged_name("a/b.mkv") == "Preview - b.mp4"


@pytest.fixture(autouse=True)
def _no_real_merge(monkeypatch):
    """Fake clips can't be merged: unless a test fakes a merge, the album path runs."""
    monkeypatch.setattr(tb, "merge_gif_previews", lambda clips, source, workdir: None)


def _fake_merge(monkeypatch, merged_clips=None):
    """Pretend ffmpeg merged the clips; records the order they were joined in."""

    def merge(clips, source, workdir):
        ordered = [path for path, _, _ in sorted(clips, key=lambda c: c[1])]
        if merged_clips is not None:
            merged_clips.extend(ordered)
        target = workdir / gif_previews.merged_name(source.name)
        target.write_bytes(b"merged")
        return target, SimpleNamespace(width=480, height=270, duration=60.0)

    monkeypatch.setattr(tb, "merge_gif_previews", merge)


def _fake_clips(tmp_path, count):
    clips = []
    for i in range(count):
        path = tmp_path / f"c{i}.mp4"
        path.write_bytes(b"clip%d" % i)
        clips.append((path, i * 60.0, SimpleNamespace(width=480, height=270, duration=6.0)))
    return clips


async def test_ten_previews_go_out_as_one_album(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "make_gif_previews", lambda source, workdir: _fake_clips(tmp_path, 10))
    context = make_context()

    await tb.send_gif_previews(context.application, 1, tmp_path / "movie.mkv")

    (album,) = context.bot.called("send_media_group")
    media = album.kwargs["media"]
    assert len(media) == 10
    assert all(item.caption is None for item in media)


async def test_a_single_preview_is_sent_as_a_video(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "make_gif_previews", lambda source, workdir: _fake_clips(tmp_path, 1))
    context = make_context()

    await tb.send_gif_previews(context.application, 1, tmp_path / "short.mp4")

    assert not context.bot.called("send_media_group")
    (video,) = context.bot.called("send_video")
    assert "caption" not in video.kwargs


async def test_a_refused_album_falls_back_to_one_by_one(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "make_gif_previews", lambda source, workdir: _fake_clips(tmp_path, 3))
    context = make_context()

    async def refuse(**kwargs):
        raise tb.BadRequest("nope")

    monkeypatch.setattr(context.bot, "send_media_group", refuse, raising=False)

    await tb.send_gif_previews(context.application, 1, tmp_path / "v.mp4")

    assert [c.kwargs.get("caption") for c in context.bot.called("send_video")] == [None, None, None]


async def test_a_timed_out_album_is_not_resent(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "make_gif_previews", lambda source, workdir: _fake_clips(tmp_path, 3))
    context = make_context()

    async def time_out(**kwargs):
        raise tb.TimedOut()

    monkeypatch.setattr(context.bot, "send_media_group", time_out, raising=False)

    await tb.send_gif_previews(context.application, 1, tmp_path / "v.mp4")

    assert not context.bot.called("send_video")


# ---------------------------------------------------------------- auto-upload filter

from app.services import upload_filter  # noqa: E402


@pytest.mark.parametrize(
    "preset, kept",
    [
        ("all", ["ep.mkv", "song.mp3", "notes.txt", "book.pdf", "pic.jpg"]),
        ("video", ["ep.mkv"]),
        ("audio", ["song.mp3"]),
        ("video_audio", ["ep.mkv", "song.mp3"]),
        ("documents", ["notes.txt", "book.pdf"]),
        ("nonsense", ["ep.mkv", "song.mp3", "notes.txt", "book.pdf", "pic.jpg"]),
    ],
)
def test_filter_presets(preset, kept):
    files = ["ep.mkv", "song.mp3", "notes.txt", "book.pdf", "pic.jpg"]
    assert upload_filter.split(preset, files) == (kept, [f for f in files if f not in kept])


def test_filter_matching_ignores_case_and_folders():
    assert upload_filter.matches("video", "Show/S01/EP.MKV")
    assert not upload_filter.matches("video", "Show.mkv/readme.nfo")
    assert upload_filter.label("bogus") == "All files"


def test_filter_defaults_to_all_files():
    assert user_settings.get_user_settings(9)["auto_upload_types"] == "all"


async def test_filter_screen_from_settings_saves_the_choice():
    _, markup = settings_views.settings_screen(user_settings.get_user_settings(1))
    assert "nav:upload_filter" in _callbacks(markup)
    context = make_context()

    await tb.on_button(callback_update(context, "nav:upload_filter"), context)
    assert "Auto-upload filter" in context.bot.texts()[-1]
    await tb.on_button(callback_update(context, "uf:video"), context)

    assert user_settings.get_user_settings(1)["auto_upload_types"] == "video"
    text, markup = settings_views.upload_filter_screen(user_settings.get_user_settings(1))
    assert "Videos only" in text
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "✅ Videos only" in labels and "All files" in labels
    assert _callbacks(markup)[:5] == [
        "uf:all",
        "uf:video",
        "uf:audio",
        "uf:video_audio",
        "uf:documents",
    ]


def _torrent(isolated, names, folder="Show"):
    (isolated / folder).mkdir()
    for name in names:
        (isolated / folder / name).write_bytes(b"x")
    job = {
        "id": 11,
        "name": folder,
        "status": "completed",
        "chat_id": 1,
        "user_id": 1,
        "started_at": 1,
        "finished_at": 2,
        "files": [str(isolated / folder / n) for n in names],
    }
    tb.download_jobs[11] = job
    return job


async def _auto(filter_value):
    await user_settings.update_setting(1, "auto_upload_after_download", True)
    await user_settings.update_setting(1, "auto_upload_types", filter_value)


def _fake_uploads(monkeypatch):
    uploads = []

    async def fake_upload(app, chat_id, message_id, files, *, title, user_id=None, auto=False):
        uploads.append((list(files), auto))

    monkeypatch.setattr(tb, "upload_files_via_pyrogram", fake_upload)
    return uploads


async def test_torrent_auto_upload_keeps_only_videos(isolated, monkeypatch):
    await _auto("video")
    job = _torrent(isolated, ["ep.mkv", "info.txt", "song.mp3"])
    uploads = _fake_uploads(monkeypatch)
    context = make_context()

    await tb.finish_job_card(context.application, job)
    await _drain_background()

    assert uploads == [(["Show/ep.mkv"], True)]
    assert sorted(p.name for p in (isolated / "Show").iterdir()) == ["ep.mkv"]
    assert "2 other file(s) deleted" in context.bot.texts()[0]


async def test_all_files_filtered_out_deletes_the_folder_and_says_so(isolated, monkeypatch):
    await _auto("video")
    job = _torrent(isolated, ["info.txt", "song.mp3", ".hidden"])
    uploads = _fake_uploads(monkeypatch)
    context = make_context()

    await tb.finish_job_card(context.application, job)
    await _drain_background()

    assert uploads == []
    assert not (isolated / "Show").exists()  # dotfile leftovers go with it
    assert "No matching files for filter Videos only, 2 file(s) deleted" in context.bot.texts()[0]


async def test_a_filtered_out_single_file_job_says_so(isolated, monkeypatch):
    await _auto("audio")
    (isolated / "clip.mp4").write_bytes(b"v")
    job = {
        "id": 12,
        "name": "clip",
        "status": "completed",
        "chat_id": 1,
        "user_id": 1,
        "provider": "yt-dlp",
        "filepath": str(isolated / "clip.mp4"),
    }
    uploads = _fake_uploads(monkeypatch)
    context = make_context()

    await tb.finish_job_card(context.application, job)

    assert uploads == [] and not (isolated / "clip.mp4").exists() and isolated.exists()
    assert "No matching files for filter Audio only, 1 file(s) deleted" in context.bot.texts()[0]


async def test_all_files_filter_changes_nothing(isolated, monkeypatch):
    await _auto("all")
    job = _torrent(isolated, ["ep.mkv", "info.txt"])
    uploads = _fake_uploads(monkeypatch)

    await tb.finish_job_card(make_context().application, job)
    await _drain_background()

    assert uploads == [(["Show/ep.mkv", "Show/info.txt"], True)]


async def test_bot_folders_are_kept_when_emptied(isolated, monkeypatch):
    await _auto("video")
    folder = tb.HENTAI_VIDEO_DIR.name
    job = _torrent(isolated, ["notes.txt"], folder=folder)
    _fake_uploads(monkeypatch)

    await tb.finish_job_card(make_context().application, job)

    assert (isolated / folder).is_dir() and not any((isolated / folder).iterdir())


async def test_manual_upload_ignores_the_filter(isolated, monkeypatch):
    await user_settings.update_setting(1, "auto_upload_types", "video")
    _torrent(isolated, ["ep.mkv", "info.txt"])
    uploads = _fake_uploads(monkeypatch)
    context = make_context()

    await tb.handle_job_callback(callback_update(context, "job:up:11"), context)
    await _drain_background()

    assert uploads == [(["Show/ep.mkv", "Show/info.txt"], False)]
    assert (isolated / "Show" / "info.txt").exists()


# ---------------------------------------------------------------- previews after auto uploads


def _logged_in(monkeypatch, sent=None):
    async def fake_send(rel_path, progress_callback=None, chat_id="me", **_):
        if sent is not None:
            sent.append(rel_path)

    async def logged_in():
        return object()

    monkeypatch.setattr(tb, "pyrogram_send_file", fake_send)
    monkeypatch.setattr(tb, "get_pyrogram_client", logged_in)


def _fake_previews(monkeypatch, fail=False):
    calls = []

    async def fake(
        app, chat_id, source, *, status_chat_id=None, fallback_chat_id=None, target=None
    ):
        calls.append(
            (chat_id, source.name, status_chat_id, fallback_chat_id, source.exists(), target)
        )
        if fail:
            raise RuntimeError("ffmpeg exploded")
        return 3

    monkeypatch.setattr(tb, "send_gif_previews", fake)
    return calls


async def test_previews_follow_each_auto_uploaded_video_only(isolated, monkeypatch):
    for name in ("a.mkv", "b.mp3", "c.txt", "d.mp4"):
        (isolated / name).write_bytes(b"x")
    _logged_in(monkeypatch)
    calls = _fake_previews(monkeypatch)
    context = make_context()

    files = ["a.mkv", "b.mp3", "c.txt", "d.mp4"]
    job = await tb.upload_files_via_pyrogram(
        context.application, 1, 2, files, title="t", user_id=1, auto=True
    )
    await _drain_background()

    assert job["status"] == "completed"
    assert [c[:4] for c in calls] == [(1, "a.mkv", 1, 1), (1, "d.mp4", 1, 1)]
    assert [c[5] for c in calls] == ["me", "me"]


async def test_previews_go_to_the_upload_target_channel(isolated, monkeypatch):
    (isolated / "a.mkv").write_bytes(b"x")
    await user_settings.update_setting(1, "target_chat_id", -100777)
    _logged_in(monkeypatch)
    calls = _fake_previews(monkeypatch)

    await tb.upload_files_via_pyrogram(
        make_context().application, 1, 2, ["a.mkv"], title="t", user_id=1, auto=True
    )
    await _drain_background()

    assert [c[:4] for c in calls] == [(-100777, "a.mkv", 1, 1)]
    assert calls[0][5] == -100777  # the merged preview follows the video


async def test_saved_messages_previews_follow_the_video(isolated, monkeypatch):
    (isolated / "a.mkv").write_bytes(b"x")
    _logged_in(monkeypatch)
    calls = _fake_previews(monkeypatch)

    await tb.upload_files_via_pyrogram(
        make_context().application, 1, 2, ["a.mkv"], title="t", user_id=1, auto=True
    )
    await _drain_background()

    assert [(c[0], c[5]) for c in calls] == [(1, "me")]


async def test_manual_uploads_send_previews_unless_turned_off(isolated, monkeypatch):
    (isolated / "a.mkv").write_bytes(b"x")
    _logged_in(monkeypatch)
    calls = _fake_previews(monkeypatch)

    await tb.upload_files_via_pyrogram(
        make_context().application, 1, 2, ["a.mkv"], title="t", user_id=1
    )
    await _drain_background()
    assert [(c[0], c[1], c[5]) for c in calls] == [(1, "a.mkv", "me")]

    await user_settings.update_setting(1, "send_previews_after_upload", False)
    calls.clear()
    await tb.upload_files_via_pyrogram(
        make_context().application, 1, 3, ["a.mkv"], title="t", user_id=1
    )
    await _drain_background()
    assert calls == []


async def test_failed_previews_still_clean_up_after_upload(isolated, monkeypatch):
    await user_settings.update_setting(1, "auto_delete_files_after_upload", True)
    (isolated / "Show").mkdir()
    (isolated / "Show" / "a.mkv").write_bytes(b"x")
    _logged_in(monkeypatch)
    calls = _fake_previews(monkeypatch, fail=True)

    job = await tb.upload_files_via_pyrogram(
        make_context().application, 1, 2, ["Show/a.mkv"], title="t", user_id=1, auto=True
    )
    await _drain_background()

    assert job["status"] == "completed"
    assert calls and calls[0][4]  # the video was still there for the previews
    assert not (isolated / "Show").exists()  # then it and its emptied folder went


async def test_previews_fall_back_to_the_bot_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "make_gif_previews", lambda source, workdir: _fake_clips(tmp_path, 3))
    context = make_context()
    albums = []

    async def media_group(chat_id, media, **kwargs):
        if chat_id == -100777:
            raise Forbidden("bot is not a member of the channel chat")
        albums.append(chat_id)

    monkeypatch.setattr(context.bot, "send_media_group", media_group, raising=False)

    sent = await tb.send_gif_previews(
        context.application, -100777, tmp_path / "v.mp4", status_chat_id=1, fallback_chat_id=1
    )

    assert sent == 3 and albums == [1]
    assert context.bot.called("send_message")[0].kwargs["chat_id"] == 1


def _user_client(monkeypatch, fail_chats=()):
    """A logged-in Pyrogram account that records send_video calls."""
    videos = []

    class Client:
        async def send_video(self, **kwargs):
            if kwargs["chat_id"] in fail_chats:
                raise RuntimeError("CHAT_WRITE_FORBIDDEN")
            videos.append(kwargs)

    client = Client()

    async def get_client():
        return client

    monkeypatch.setattr(tb, "get_pyrogram_client", get_client)
    return videos


async def test_one_merged_preview_in_timeline_order(tmp_path, monkeypatch):
    clips = _fake_clips(tmp_path, 10)
    shuffled = clips[5:] + clips[:5]
    monkeypatch.setattr(tb, "make_gif_previews", lambda source, workdir: shuffled)
    joined = []
    _fake_merge(monkeypatch, joined)
    context = make_context()

    sent = await tb.send_gif_previews(context.application, 1, tmp_path / "movie.mkv")

    assert sent == 10
    assert joined == [path for path, _, _ in clips]
    assert not context.bot.called("send_media_group")
    (video,) = context.bot.called("send_video")
    assert "caption" not in video.kwargs
    assert video.kwargs["video"].name.endswith("Preview - movie.mp4")


@pytest.mark.parametrize("target", [-100777, "me"])
async def test_merged_preview_goes_to_the_upload_target(tmp_path, monkeypatch, target):
    monkeypatch.setattr(tb, "make_gif_previews", lambda source, workdir: _fake_clips(tmp_path, 3))
    _fake_merge(monkeypatch)
    videos = _user_client(monkeypatch)
    workdirs = []
    real_tmp = tb.tempfile.TemporaryDirectory

    def tracked(**kwargs):
        tmp = real_tmp(**kwargs)
        workdirs.append(tb.Path(tmp.name))
        return tmp

    monkeypatch.setattr(tb.tempfile, "TemporaryDirectory", tracked)
    context = make_context()

    sent = await tb.send_gif_previews(
        context.application,
        target if isinstance(target, int) else 1,
        tmp_path / "v.mp4",
        status_chat_id=1,
        fallback_chat_id=1,
        target=target,
    )

    assert sent == 3
    (video,) = videos
    assert video["chat_id"] == target  # same chat as the uploaded video, not the bot chat
    assert video["file_name"] == "Preview - v.mp4"
    assert video.get("caption") is None
    assert not context.bot.called("send_video") and not context.bot.called("send_media_group")
    assert workdirs and not any(d.exists() for d in workdirs)  # clips and merged file are gone


async def test_bot_only_mode_sends_the_merged_preview_like_the_video(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "make_gif_previews", lambda source, workdir: _fake_clips(tmp_path, 3))
    _fake_merge(monkeypatch)

    async def unavailable():
        raise tb.PyrogramUnavailable("no login")

    monkeypatch.setattr(tb, "get_pyrogram_client", unavailable)
    context = make_context()

    await tb.send_gif_previews(
        context.application,
        1,
        tmp_path / "v.mp4",
        status_chat_id=1,
        fallback_chat_id=1,
        target="me",
    )
    await tb.send_gif_previews(
        context.application,
        -100777,
        tmp_path / "v.mp4",
        status_chat_id=1,
        fallback_chat_id=1,
        target=-100777,
    )

    assert [v.kwargs["chat_id"] for v in context.bot.called("send_video")] == [1, -100777]


async def test_merged_preview_falls_back_to_the_bot_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "make_gif_previews", lambda source, workdir: _fake_clips(tmp_path, 3))
    _fake_merge(monkeypatch)
    videos = _user_client(monkeypatch, fail_chats=(-100777,))
    context = make_context()

    sent = await tb.send_gif_previews(
        context.application,
        -100777,
        tmp_path / "v.mp4",
        status_chat_id=1,
        fallback_chat_id=1,
        target=-100777,
    )

    assert sent == 3 and videos == []
    (video,) = context.bot.called("send_video")
    assert video.kwargs["chat_id"] == 1


async def test_failed_merge_falls_back_to_the_album(tmp_path, monkeypatch):
    monkeypatch.setattr(tb, "make_gif_previews", lambda source, workdir: _fake_clips(tmp_path, 3))
    monkeypatch.setattr(tb, "merge_gif_previews", REAL_MERGE)  # the real merge, with no ffmpeg
    monkeypatch.setattr(tb, "FFMPEG_BIN", "/nonexistent/ffmpeg")
    videos = _user_client(monkeypatch)
    context = make_context()

    sent = await tb.send_gif_previews(
        context.application,
        -100777,
        tmp_path / "v.mp4",
        status_chat_id=1,
        fallback_chat_id=1,
        target=-100777,
    )

    assert sent == 3 and videos == []
    (album,) = context.bot.called("send_media_group")
    assert album.kwargs["chat_id"] == -100777 and len(album.kwargs["media"]) == 3
    assert all(item.caption is None for item in album.kwargs["media"])


async def test_preview_button_sends_one_merged_video(isolated, monkeypatch):
    (isolated / "v.mp4").write_bytes(b"x")
    monkeypatch.setattr(tb, "make_gif_previews", lambda source, workdir: _fake_clips(isolated, 4))
    _fake_merge(monkeypatch)
    context = make_context()

    await tb.on_button(callback_update(context, f"fb:gifprev:0:{tb.encode_path('v.mp4')}"), context)
    await _drain_background()

    assert not context.bot.called("send_media_group")
    (video,) = context.bot.called("send_video")
    assert "caption" not in video.kwargs


async def test_playlist_upload_and_delete_applies_the_filter(isolated, monkeypatch):
    await user_settings.update_setting(1, "auto_upload_types", "video")
    outputs = iter(["ep1.mp4", "ep2.mp3"])
    sent = []
    _logged_in(monkeypatch, sent)
    calls = _fake_previews(monkeypatch)

    async def fake_ytdlp(app, chat_id, url, **kwargs):
        path = isolated / next(outputs)
        path.write_bytes(b"x")
        return {"status": "completed", "filepath": str(path)}

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(tb, "start_ytdlp_download", fake_ytdlp)
    monkeypatch.setattr(tb, "maybe_auto_update_status_message", quiet)
    job = {
        "id": 13,
        "chat_id": 1,
        "user_id": 1,
        "status": "downloading",
        "batch_download_mode": "upload_and_delete",
    }

    await tb.run_video_batch(
        make_context().application, 1, ["u1", "u2"], job, item_label="Episode", user_id=1
    )
    await _drain_background()

    assert sent == ["ep1.mp4"]
    assert [c[1] for c in calls] == ["ep1.mp4"]
    assert not any(isolated.iterdir())  # mp3 filtered out, mp4 deleted after its previews
    assert job["filtered_items"] == 1 and job["uploaded_items"] == 1
    assert "1 item(s) deleted by the auto-upload filter" in job["auto_upload_note"]
