"""Playable video splitting for uploads, the Premium part cap, and Compress (H.265)."""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.bot.telegram_bot as tb
from app.bot.views import files as file_views
from app.bot.views import jobs as job_views
from app.bot.views import settings as settings_views
from app.services import user_settings, video_compress, video_split
from tests.fakes import callback_update, make_context

MIB = 1024 * 1024
REAL_PICK_ENCODER = video_compress.pick_encoder


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", downloads)
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", settings_dir)
    monkeypatch.setattr(tb, "upload_jobs", {})
    monkeypatch.setattr(tb, "compress_jobs", {})
    monkeypatch.setattr(video_compress, "pick_encoder", lambda ffmpeg="ffmpeg": "libx265")
    return downloads


async def _drain_background():
    while tb.background_tasks:
        await asyncio.gather(*list(tb.background_tasks), return_exceptions=True)
        await asyncio.sleep(0)


def _callback_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


# ---------------------------------------------------------------- part size


def test_part_cap_is_below_telegrams_limit_and_doubles_with_premium():
    assert video_split.part_cap(False) == 1950 * MIB
    assert video_split.part_cap(True) == 3900 * MIB
    # Both stay under Telegram's 2000 / 4000 MiB.
    assert video_split.part_cap(False) < 2000 * MIB
    assert video_split.part_cap(True) < 4000 * MIB


def test_parts_aim_at_about_1900_mib():
    target = video_split.target_part_bytes(video_split.DEFAULT_PART_CAP)
    assert 1890 * MIB < target < 1910 * MIB
    assert video_split.target_part_bytes(video_split.PREMIUM_PART_CAP) == 2 * target


def test_segment_time_comes_from_the_average_bitrate():
    # 5000 MiB over 5000 s is 1 MiB/s: ~1901 s per 1901 MiB part.
    target = video_split.target_part_bytes(video_split.DEFAULT_PART_CAP)
    seconds = video_split.segment_seconds(5000 * MIB, 5000.0, target)
    assert seconds == int(target / MIB)
    assert video_split.expected_parts(5000 * MIB, target) == 3
    # A short, dense video never gets segments under the minimum.
    assert (
        video_split.segment_seconds(10_000 * MIB, 20.0, target) == video_split.MIN_SEGMENT_SECONDS
    )
    with pytest.raises(ValueError):
        video_split.segment_seconds(100, 0, target)


# ---------------------------------------------------------------- split command


def test_segment_command_copies_streams_into_faststart_mp4_parts(tmp_path):
    cmd = video_split.build_segment_command(
        "ffmpeg", tmp_path / "movie.mp4", tmp_path / "segment_%03d.mp4", 1234, ".mp4"
    )
    joined = " ".join(cmd)
    assert cmd[0] == "ffmpeg"
    assert "-c copy" in joined
    assert "-f segment" in joined
    assert "-segment_time 1234" in joined
    assert "-reset_timestamps 1" in joined
    assert "-map 0:v:0 -map 0:a?" in joined
    # faststart has to reach the inner mp4 muxer; a bare -movflags is ignored there.
    assert "-segment_format_options movflags=+faststart" in joined
    assert cmd[-1] == str(tmp_path / "segment_%03d.mp4")


def test_non_mp4_containers_keep_their_format_without_faststart(tmp_path):
    assert video_split.part_suffix(Path("a.MOV")) == ".mp4"
    assert video_split.part_suffix(Path("a.mkv")) == ".mkv"
    cmd = video_split.build_segment_command(
        "ffmpeg", Path("a.mkv"), tmp_path / "segment_%03d.mkv", 60, ".mkv"
    )
    assert "-segment_format_options" not in cmd


def _fake_ffmpeg(sizes_per_attempt, calls):
    """A subprocess.run stand-in that writes segments of the given sizes."""

    def run(cmd, **_):
        calls.append(cmd)
        sizes = sizes_per_attempt[len(calls) - 1]
        pattern = cmd[-1]
        for i, size in enumerate(sizes, start=1):
            with open(pattern % i, "wb") as fh:
                fh.truncate(size)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return run


def test_split_video_names_parts_and_shrinks_segments_after_an_overshoot(tmp_path, monkeypatch):
    source = tmp_path / "50% [clip].mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr(
        video_split, "probe", lambda path, ffprobe="ffprobe": video_split.MediaInfo(300.0, 3000)
    )
    calls = []
    run = _fake_ffmpeg([[1200, 900, 900], [950, 950, 950, 150]], calls)

    parts = video_split.split_video(source, tmp_path / "out", 1000, run=run)

    assert [p.name for p in parts] == [f"50% [clip]_part_{i:03d}.mp4" for i in range(1, 5)]
    assert all(p.stat().st_size <= 1000 for p in parts)
    first, second = (int(c[c.index("-segment_time") + 1]) for c in calls)
    assert second < first
    # No ffmpeg-pattern leftovers from the first, oversized attempt.
    assert not list((tmp_path / "out").glob("segment_*"))


def test_split_video_gives_up_when_duration_is_unknown_or_ffmpeg_fails(tmp_path, monkeypatch):
    source = tmp_path / "a.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr(
        video_split, "probe", lambda path, ffprobe="ffprobe": video_split.MediaInfo(None, 3000)
    )
    assert video_split.split_video(source, tmp_path / "out", 1000, run=None) is None

    monkeypatch.setattr(
        video_split, "probe", lambda path, ffprobe="ffprobe": video_split.MediaInfo(60.0, 3000)
    )
    failed = lambda cmd, **_: subprocess.CompletedProcess(cmd, 1, "", "boom")  # noqa: E731
    assert video_split.split_video(source, tmp_path / "out", 1000, run=failed) is None


# ---------------------------------------------------------------- choosing how to split


def test_big_videos_split_playable_and_other_files_as_document_chunks(monkeypatch):
    video = tb.DOWNLOAD_DIR / "movie.mp4"
    video.write_bytes(b"v" * 30)
    other = tb.DOWNLOAD_DIR / "disk.iso"
    other.write_bytes(b"d" * 30)
    small = tb.DOWNLOAD_DIR / "small.mp4"
    small.write_bytes(b"s" * 5)
    seen = {}

    def fake_split(source, out_dir, cap, **_):
        seen["cap"] = cap
        out_dir.mkdir()
        parts = [out_dir / f"{source.stem}_part_{i:03d}.mp4" for i in (1, 2, 3)]
        for p in parts:
            p.write_bytes(b"p" * 10)
        return parts

    monkeypatch.setattr(video_split, "split_video", fake_split)

    parts, as_document = tb.prepare_upload_parts("movie.mp4", 12)
    assert seen["cap"] == 12 and as_document is False
    assert [(Path(p).name, i, n) for p, i, n in parts] == [
        ("movie_part_001.mp4", 1, 3),
        ("movie_part_002.mp4", 2, 3),
        ("movie_part_003.mp4", 3, 3),
    ]

    parts, as_document = tb.prepare_upload_parts("disk.iso", 12)
    assert as_document is True and len(parts) == 3
    assert [Path(p).stat().st_size for p, _, _ in parts] == [12, 12, 6]
    tb.cleanup_file_chunks(parts)

    assert tb.prepare_upload_parts("small.mp4", 12) == ([(str(small), 1, 1)], False)


def test_a_video_ffmpeg_cannot_cut_falls_back_to_document_chunks(monkeypatch):
    (tb.DOWNLOAD_DIR / "odd.mp4").write_bytes(b"v" * 30)
    monkeypatch.setattr(video_split, "split_video", lambda *a, **k: None)

    parts, as_document = tb.prepare_upload_parts("odd.mp4", 12)

    assert as_document is True and len(parts) == 3
    tb.cleanup_file_chunks(parts)


# ---------------------------------------------------------------- premium cap at upload time


class _Client:
    def __init__(self, premium):
        self.premium = premium
        self.sent = []

    async def get_me(self):
        if self.premium is None:
            raise ConnectionError("offline")
        return SimpleNamespace(is_premium=self.premium, is_bot=False)

    async def send_video(self, **kwargs):
        self.sent.append(("video", kwargs))

    async def send_document(self, **kwargs):
        self.sent.append(("document", kwargs))


@pytest.mark.parametrize(
    "premium, cap",
    [(True, 3900 * MIB), (False, 1950 * MIB), (None, 1950 * MIB)],
)
async def test_upload_cap_follows_premium(premium, cap):
    assert await tb.upload_part_cap(_Client(premium)) == cap


@pytest.mark.parametrize("premium", [True, False])
async def test_upload_uses_the_accounts_cap_for_splitting_and_sending(monkeypatch, premium):
    (tb.DOWNLOAD_DIR / "movie.mp4").write_bytes(b"x")
    client = _Client(premium)
    seen = {}

    async def get_client():
        return client

    def fake_prepare(rel_path, cap):
        seen["prepare_cap"] = cap
        return [(str(tb.DOWNLOAD_DIR / rel_path), 1, 1)], False

    async def fake_send(rel_path, progress_callback=None, chat_id="me", **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(tb, "get_pyrogram_client", get_client)
    monkeypatch.setattr(tb, "prepare_upload_parts", fake_prepare)
    monkeypatch.setattr(tb, "pyrogram_send_file", fake_send)
    context = make_context()

    job = await tb.upload_files_via_pyrogram(context.application, 1, 5, ["movie.mp4"], title="t")

    expected = video_split.part_cap(premium)
    assert job["status"] == "completed"
    assert seen == {"prepare_cap": expected, "max_size": expected, "force_document": False}


async def test_each_video_part_is_sent_as_a_streamable_video_and_byte_parts_as_documents(
    monkeypatch,
):
    (tb.DOWNLOAD_DIR / "part.mp4").write_bytes(b"x" * 10)
    client = _Client(False)

    async def get_client():
        return client

    monkeypatch.setattr(tb, "get_pyrogram_client", get_client)
    monkeypatch.setattr(tb, "generate_thumbnail", lambda path: "")
    monkeypatch.setattr(
        tb, "get_video_metadata", lambda path: {"width": 1280, "height": 720, "duration": 60}
    )

    await tb.pyrogram_send_file("part.mp4", max_size=100)
    await tb.pyrogram_send_file("part.mp4", force_document=True, max_size=100)
    with pytest.raises(ValueError):
        await tb.pyrogram_send_file("part.mp4", max_size=5)

    (kind1, video), (kind2, _) = client.sent
    assert (kind1, kind2) == ("video", "document")
    assert video["supports_streaming"] is True
    assert (video["width"], video["height"], video["duration"]) == (1280, 720, 60)


@pytest.mark.parametrize(
    "meta",
    [
        {"width": None, "height": None, "duration": None},
        {"width": "N/A", "height": "", "duration": "N/A"},
        {"width": "1280", "height": 720.0, "duration": "61.7"},
        {"width": -1, "height": 0, "duration": float("nan")},
        {},
    ],
)
async def test_unreadable_video_metadata_still_sends(monkeypatch, meta):
    # An MP4 still downloading has no moov atom: ffprobe gives no duration,
    # and Pyrogram crashed packing None as a Double (struct.error).
    from pyrogram import raw

    (tb.DOWNLOAD_DIR / "part.mp4").write_bytes(b"x" * 10)
    client = _Client(False)

    async def get_client():
        return client

    monkeypatch.setattr(tb, "get_pyrogram_client", get_client)
    monkeypatch.setattr(tb, "generate_thumbnail", lambda path: "")
    monkeypatch.setattr(tb, "get_video_metadata", lambda path: meta)

    await tb.pyrogram_send_file("part.mp4", max_size=100)

    [(kind, video)] = client.sent
    assert kind == "video"
    for key in ("width", "height", "duration"):
        assert key not in video or (isinstance(video[key], int) and video[key] > 0)
    # What Pyrogram would build from these arguments must serialize.
    raw.types.DocumentAttributeVideo(
        duration=video.get("duration", 0),
        w=video.get("width", 0),
        h=video.get("height", 0),
        supports_streaming=True,
    ).write()


def test_video_metadata_of_a_file_ffprobe_cannot_read(monkeypatch):
    out = "width=N/A\nheight=720\nduration=N/A\n"
    monkeypatch.setattr(
        tb.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=out, returncode=0)
    )
    assert tb.get_video_metadata("x.mp4") == {"width": None, "height": 720, "duration": None}
    assert tb.video_send_kwargs(tb.get_video_metadata("x.mp4")) == {"height": 720}


# ---------------------------------------------------------------- compress command


def test_compress_command_x265_with_audio_copy(tmp_path):
    cmd = video_compress.build_compress_command(
        "ffmpeg",
        tmp_path / "a.mkv",
        tmp_path / "a_x265.mp4",
        encoder="libx265",
        crf=28,
        preset="slow",
        copy_audio=True,
    )
    joined = " ".join(cmd)
    assert "-c:v libx265 -crf 28 -preset slow" in joined
    assert "-tag:v hvc1" in joined
    assert "-c:a copy" in joined
    assert "-movflags +faststart" in joined
    assert "-progress pipe:1" in joined
    assert cmd[-1] == str(tmp_path / "a_x265.mp4")


def test_compress_command_x264_fallback_reencodes_audio(tmp_path):
    cmd = video_compress.build_compress_command(
        "ffmpeg",
        tmp_path / "a.mkv",
        tmp_path / "a_x264.mp4",
        encoder="libx264",
        crf=26,
        preset="medium",
        copy_audio=False,
    )
    joined = " ".join(cmd)
    assert "-c:v libx264 -crf 26" in joined
    assert "hvc1" not in joined
    assert "-c:a aac" in joined


def test_audio_copy_only_for_codecs_mp4_can_hold():
    assert video_compress.can_copy_audio(["aac"])
    assert video_compress.can_copy_audio([])  # silent video
    assert not video_compress.can_copy_audio(["aac", "pcm_s16le"])
    assert not video_compress.can_copy_audio(None)  # ffprobe failed


def test_encoder_falls_back_to_x264(monkeypatch):
    REAL_PICK_ENCODER.cache_clear()
    listing = {
        "x264": " V....D libx264  H.264\n",
        "x265": " V....D libx264 H.264\n V....D libx265 HEVC\n",
    }
    fake = lambda cmd, **_: subprocess.CompletedProcess(cmd, 0, listing[cmd[0]], "")  # noqa: E731
    monkeypatch.setattr(video_compress.subprocess, "run", fake)
    assert REAL_PICK_ENCODER("x264") == "libx264"
    assert REAL_PICK_ENCODER("x265") == "libx265"
    REAL_PICK_ENCODER.cache_clear()


def test_crf_and_preset_settings_cycle_and_normalize():
    assert video_compress.next_crf(28) == 30
    assert video_compress.next_crf(32) == 24
    assert video_compress.next_preset("medium") == "slow"
    assert video_compress.next_preset("slow") == "fast"
    assert video_compress.normalize_crf("junk") == 28
    assert video_compress.normalize_preset("ultrafast-ish") == "medium"


# ---------------------------------------------------------------- compress buttons and job


def test_compress_button_on_video_file_and_job_cards():
    _, markup = file_views.file_screen("a.mp4", 10, 0, 0, lambda p: "t", is_video=True)
    assert "fb:compress:0:t" in _callback_data(markup)
    _, markup = file_views.file_screen("a.zip", 10, 0, 0, lambda p: "t", is_video=False)
    assert "fb:compress:0:t" not in _callback_data(markup)

    job = {"id": 7, "name": "clip", "provider": "yt-dlp", "status": "completed"}
    _, markup = job_views.job_card(job, can_upload=True, can_compress=True)
    assert "job:cmp:7" in _callback_data(markup)
    _, markup = job_views.job_card(job, can_upload=True)
    assert "job:cmp:7" not in _callback_data(markup)


async def test_compress_settings_buttons_cycle_and_are_used_by_jobs():
    assert {"compress_crf", "compress_preset"} <= settings_views.HUB_CYCLES
    context = make_context()
    await tb.on_button(callback_update(context, "set:compress_crf"), context)
    await tb.on_button(callback_update(context, "set:compress_preset"), context)

    saved = user_settings.get_user_settings(1)
    assert (saved["compress_crf"], saved["compress_preset"]) == (30, "slow")
    assert "Compress CRF: 30" in str(context.bot.calls[-1].kwargs["reply_markup"])

    job = tb.new_compress_job(1, 2, 1, "a.mp4", None)
    assert (job["crf"], job["preset"], job["codec"]) == (30, "slow", "H.265")


def _fake_compress(calls, *, size=40):
    async def compress(source, target, *, crf, preset, progress_callback=None, on_process=None):
        calls.append((source.name, target.name, crf, preset))
        await progress_callback(50)
        target.write_bytes(b"c" * size)
        return "libx265"

    return compress


async def test_compress_from_the_file_card_then_replace_the_original(monkeypatch):
    (tb.DOWNLOAD_DIR / "clip.mkv").write_bytes(b"o" * 100)
    calls = []
    monkeypatch.setattr(tb, "compress_video", _fake_compress(calls))
    context = make_context()
    token = tb.encode_path("clip.mkv")

    await tb.on_button(callback_update(context, f"fb:compress:0:{token}"), context)
    await _drain_background()

    assert calls == [("clip.mkv", "clip_x265.mp4", 28, "medium")]
    (job,) = tb.compress_jobs.values()
    assert job["status"] == "done" and job["output"] == "clip_x265.mp4"
    assert (job["original_size"], job["new_size"]) == (100, 40)
    final = context.bot.calls[-1].kwargs
    assert "100 B → 40 B (−60%)" in final["text"]
    data = _callback_data(final["reply_markup"])
    assert {f"cmp:up:{job['id']}", f"cmp:rep:{job['id']}", f"fb:file:0:{token}"} <= set(data)

    # Replace asks first, then swaps the files.
    await tb.handle_compress_callback(callback_update(context, f"cmp:rep:{job['id']}"), context)
    assert f"cmp:repy:{job['id']}" in _callback_data(context.bot.calls[-1].kwargs["reply_markup"])
    await tb.handle_compress_callback(callback_update(context, f"cmp:repy:{job['id']}"), context)

    assert job["status"] == "replaced"
    assert not (tb.DOWNLOAD_DIR / "clip.mkv").exists()
    assert not (tb.DOWNLOAD_DIR / "clip_x265.mp4").exists()
    assert (tb.DOWNLOAD_DIR / "clip.mp4").read_bytes() == b"c" * 40


async def test_compress_from_a_job_card_and_upload_the_result(monkeypatch):
    (tb.DOWNLOAD_DIR / "v.mp4").write_bytes(b"o" * 100)
    monkeypatch.setattr(
        tb,
        "download_jobs",
        {
            3: {
                "id": 3,
                "chat_id": 1,
                "user_id": 1,
                "status": "completed",
                "provider": "yt-dlp",
                "filepath": str(tb.DOWNLOAD_DIR / "v.mp4"),
            },
        },
    )
    monkeypatch.setattr(tb, "compress_video", _fake_compress([]))
    uploads = []

    async def fake_upload(app, chat_id, message_id, rel_path, user_id=None):
        uploads.append(rel_path)

    monkeypatch.setattr(tb, "send_single_file_via_pyrogram", fake_upload)
    context = make_context()

    _, markup = tb.render_job_card(tb.download_jobs[3])
    assert "job:cmp:3" in _callback_data(markup)
    await tb.handle_job_callback(callback_update(context, "job:cmp:3"), context)
    await _drain_background()
    (job,) = tb.compress_jobs.values()
    assert job["status"] == "done"

    await tb.handle_compress_callback(callback_update(context, f"cmp:up:{job['id']}"), context)
    await _drain_background()
    assert uploads == ["v_x265.mp4"]
    assert (tb.DOWNLOAD_DIR / "v.mp4").exists()  # kept: compressing adds a file


async def test_cancelling_a_compression_stops_it(monkeypatch):
    (tb.DOWNLOAD_DIR / "c.mp4").write_bytes(b"o" * 100)
    started = asyncio.Event()
    killed = asyncio.Event()
    process = SimpleNamespace(returncode=None, kill=killed.set)

    async def slow_compress(
        source, target, *, crf, preset, progress_callback=None, on_process=None
    ):
        on_process(process)
        started.set()
        await asyncio.wait_for(killed.wait(), 5)
        raise RuntimeError("ffmpeg was stopped")

    monkeypatch.setattr(tb, "compress_video", slow_compress)
    context = make_context()
    job = await tb.start_compress_job(context.application, 1, 1, "c.mp4")
    await asyncio.wait_for(started.wait(), 5)
    assert job["status"] == "encoding"

    await tb.handle_compress_callback(callback_update(context, f"cmp:cancel:{job['id']}"), context)
    await _drain_background()

    assert killed.is_set()
    assert job["status"] == "cancelled"
    assert "Compression cancelled" in context.bot.texts()[-1]


async def test_failed_compression_shows_the_error(monkeypatch):
    (tb.DOWNLOAD_DIR / "f.mp4").write_bytes(b"o")

    async def broken(*_, **__):
        raise RuntimeError("ffmpeg failed: bad data")

    monkeypatch.setattr(tb, "compress_video", broken)
    context = make_context()
    job = await tb.start_compress_job(context.application, 1, 1, "f.mp4")
    await _drain_background()

    assert job["status"] == "failed"
    assert "bad data" in context.bot.texts()[-1]
