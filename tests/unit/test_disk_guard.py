"""Disk guard: free-space checks, FIFO purge, and how downloads react to it."""

import asyncio
import functools
import os
import time

import pytest

import app.bot.telegram_bot as tb
from app.bot.views import jobs as job_views
from app.services import disk_guard, user_settings
from app.services.download_slots import DownloadSlots

GB = disk_guard.GB
MB = 1024**2
OLD = time.time() - 3600  # older than RECENT_SECONDS


class FakeDisk:
    """Free space = capacity minus the bytes of every file under ``root``."""

    def __init__(self, root, capacity):
        self.root = root
        self.capacity = capacity
        self.calls = 0

    def used(self):
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def __call__(self, path):
        self.calls += 1
        return self.capacity - self.used()


def write(root, rel, size, mtime=OLD):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.truncate(size)  # sparse: FakeDisk counts st_size, not blocks
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def root(tmp_path):
    folder = tmp_path / "Download"
    folder.mkdir()
    return folder


# ------------------------------------------------------------ helpers


def test_required_bytes_adds_buffer_and_clamps_negatives():
    assert disk_guard.required_bytes(5 * GB) == 8 * GB
    assert disk_guard.required_bytes(None) == 3 * GB
    assert disk_guard.required_bytes(2 * GB, buffer=0) == 2 * GB
    assert disk_guard.required_bytes(-5, buffer=-1) == 0


def test_space_check_messages():
    check = disk_guard.SpaceCheck(ok=False, needed=8 * GB, free=int(2.5 * GB))
    assert check.message() == "Insufficient disk space (needs 8.0 GB, 2.5 GB free)"
    assert check.purge_note() is None
    check.purged = [object()]
    check.reclaimed = GB
    assert check.purge_note() == "Freed 1.0 GB by removing 1 old file"
    check.purged.append(object())
    assert check.purge_note() == "Freed 1.0 GB by removing 2 old files"


def test_free_bytes_reads_the_real_disk(root):
    assert disk_guard.free_bytes(root) > 0


def test_allocated_bytes_skips_missing_files(root):
    real = root / "a.bin"
    real.write_bytes(b"x" * 8192)
    assert disk_guard.allocated_bytes([real, root / "missing"]) >= 8192
    assert disk_guard.allocated_bytes([]) == 0


# ------------------------------------------------------------ candidates


@pytest.mark.parametrize(
    "name",
    [
        "movie.mkv.part",
        "a.ytdl",
        "x.tmp",
        "y.temp",
        "z.crdownload",
        "w.download",
        "v.incomplete",
        "clip.f137.mp4.part-Frag12",
        "file.aria2",
    ],
)
def test_partial_downloads_are_incomplete(root, name):
    assert disk_guard.is_incomplete(write(root, name, 10), root)


def test_aria2_control_file_marks_file_and_folder_incomplete(root):
    movie = write(root, "Movie.mkv", 10)
    write(root, "Movie.mkv.aria2", 1)
    episode = write(root, "Show/S01/e1.mkv", 10)
    write(root, "Show.aria2", 1)
    assert disk_guard.is_incomplete(movie, root)
    assert disk_guard.is_incomplete(episode, root)
    assert not disk_guard.is_incomplete(write(root, "done.mkv", 10), root)


def test_protected_path_and_children(root):
    folder = root / "Keep"
    assert disk_guard.is_protected(folder, [folder])
    assert disk_guard.is_protected(folder / "a" / "b.mkv", [folder])
    assert not disk_guard.is_protected(root / "Keeper.mkv", [folder])


def test_candidates_are_oldest_first_and_skip_unsafe_files(root):
    now = time.time()
    newest = write(root, "c.mkv", 1, mtime=now - 2000)
    oldest = write(root, "sub/a.mkv", 1, mtime=now - 9000)
    middle = write(root, "b.mkv", 1, mtime=now - 5000)
    write(root, "recent.mkv", 1, mtime=now - 60)  # may still be written
    write(root, ".hidden", 1)
    write(root, ".cache/x.bin", 1)
    write(root, "partial.mkv.part", 1)
    write(root, "Keep/k.mkv", 1)
    (root / "link.mkv").symlink_to(oldest)

    found = disk_guard.purge_candidates(root, [root / "Keep"], now=now)

    assert [path for _, _, path in found] == [oldest.resolve(), middle.resolve(), newest.resolve()]


def test_candidates_ties_break_by_path(root):
    b = write(root, "b.mkv", 1)
    a = write(root, "a.mkv", 1)
    assert [p for _, _, p in disk_guard.purge_candidates(root)] == [a.resolve(), b.resolve()]


def test_min_age_zero_allows_fresh_files(root):
    fresh = write(root, "fresh.mkv", 1, mtime=time.time())
    assert disk_guard.purge_candidates(root) == []
    assert [p for _, _, p in disk_guard.purge_candidates(root, min_age=0)] == [fresh.resolve()]


# ------------------------------------------------------------ ensure_space


def test_enough_space_purges_nothing(root):
    old = write(root, "old.mkv", 1 * GB)
    disk = FakeDisk(root, 10 * GB)
    check = disk_guard.ensure_space(root, 2 * GB, free_fn=disk)
    assert check.ok and check.purged == [] and old.exists()
    assert check.needed == 5 * GB and check.free == 9 * GB
    assert disk.calls == 1


def test_low_disk_refused_without_auto_purge(root):
    old = write(root, "old.mkv", 6 * GB)
    check = disk_guard.ensure_space(root, 2 * GB, auto_purge=False, free_fn=FakeDisk(root, 8 * GB))
    assert not check.ok
    assert old.exists()
    assert check.message() == "Insufficient disk space (needs 5.0 GB, 2.0 GB free)"


def test_fifo_purge_stops_once_enough_is_free(root):
    now = time.time()
    first = write(root, "first.mkv", 2 * GB, mtime=now - 9000)
    second = write(root, "Show/second.mkv", 2 * GB, mtime=now - 8000)
    third = write(root, "third.mkv", 2 * GB, mtime=now - 7000)
    disk = FakeDisk(root, 8 * GB)  # 2 GB free

    check = disk_guard.ensure_space(root, 2 * GB, free_fn=disk, now=now)  # needs 5 GB

    assert check.ok
    assert not first.exists() and not second.exists() and third.exists()
    assert check.purged == [first.resolve(), second.resolve()]
    assert check.reclaimed == 4 * GB and check.free == 6 * GB
    assert not (root / "Show").exists()  # emptied folder pruned
    assert check.purge_note() == "Freed 4.0 GB by removing 2 old files"


def test_purge_keeps_kept_folders_and_root(root):
    write(root, "Telegram/a.mkv", 2 * GB)
    check = disk_guard.ensure_space(
        root, 0, buffer=GB, free_fn=FakeDisk(root, 2 * GB), keep_dirs=[root / "Telegram"]
    )
    assert check.ok
    assert (root / "Telegram").is_dir() and root.is_dir()


def test_purge_that_cannot_free_enough_refuses(root):
    old = write(root, "old.mkv", 1 * GB)
    busy = write(root, "busy.mkv", 4 * GB)
    write(root, "busy.mkv.aria2", 0)
    kept = write(root, "Keep/k.mkv", 2 * GB)

    check = disk_guard.ensure_space(
        root, 4 * GB, protected=[root / "Keep"], free_fn=FakeDisk(root, 8 * GB)
    )

    assert not check.ok
    assert not old.exists() and busy.exists() and kept.exists()
    assert check.purged == [old.resolve()]
    assert check.free == 2 * GB and check.needed == 7 * GB
    assert "needs 7.0 GB, 2.0 GB free" in check.message()


def test_purge_survives_files_vanishing_or_undeletable(root, monkeypatch):
    gone = write(root, "a.mkv", GB)
    locked = write(root, "b.mkv", GB)
    good = write(root, "c.mkv", GB)
    real_unlink = type(gone).unlink

    def unlink(self, *args, **kwargs):
        if self.name == "a.mkv":
            raise FileNotFoundError(self)
        if self.name == "b.mkv":
            raise PermissionError(self)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(type(gone), "unlink", unlink)
    check = disk_guard.ensure_space(root, 0, buffer=GB, free_fn=FakeDisk(root, 3 * GB))
    assert check.ok
    assert check.purged == [good.resolve()]
    assert locked.exists()


def test_empty_disk_with_nothing_to_purge(root):
    check = disk_guard.ensure_space(root, 10 * GB, free_fn=lambda _: GB)
    assert not check.ok and check.purged == [] and check.free == GB


# ------------------------------------------------------------ bot integration


@pytest.fixture
def bot_disk(tmp_path, monkeypatch):
    """The bot's guard turned on against a temporary Download/ with a fake disk."""
    folder = tmp_path / "Download"
    folder.mkdir()
    disk = FakeDisk(folder, 10 * GB)
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", folder)
    monkeypatch.setattr(tb, "DISK_GUARD_ENABLE", True)
    monkeypatch.setattr(tb, "DISK_GUARD_AUTO_PURGE", True)
    monkeypatch.setattr(tb, "DISK_GUARD_BUFFER_GB", 3)
    monkeypatch.setattr(tb, "DISK_GUARD_PROTECTED", ["_torrents"])
    monkeypatch.setattr(tb, "download_jobs", {})
    monkeypatch.setattr(tb, "upload_jobs", {})
    monkeypatch.setattr(tb, "compress_jobs", {})
    monkeypatch.setattr(tb, "download_slots", DownloadSlots())
    monkeypatch.setattr(user_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(
        disk_guard, "ensure_space", functools.partial(disk_guard.ensure_space, free_fn=disk)
    )
    updates, finished = [], []

    async def update(app, job, force=False):
        updates.append((job.get("status"), force))

    async def finish(app, job):
        finished.append(job.get("status"))

    monkeypatch.setattr(tb, "maybe_auto_update_status_message", update)
    monkeypatch.setattr(tb, "finish_job_card", finish)
    disk.updates, disk.finished = updates, finished
    return disk


def _job(**overrides):
    job = {"id": 1, "user_id": 1, "chat_id": 1, "name": "x", "status": "starting"}
    job.update(overrides)
    tb.download_jobs[job["id"]] = job
    return job


async def test_check_disk_space_is_none_when_guard_off(bot_disk, monkeypatch):
    monkeypatch.setattr(tb, "DISK_GUARD_ENABLE", False)
    assert await tb.check_disk_space(100 * GB) is None


async def test_check_disk_space_never_purges_active_job_files(bot_disk):
    root = bot_disk.root
    active = write(root, "Busy/part1.mkv", 3 * GB)
    write(root, "_torrents/x.torrent", 1 * GB)
    old = write(root, "old.mkv", 3 * GB)
    _job(status="downloading", folder=str(root / "Busy"))

    check = await tb.check_disk_space(2 * GB)  # needs 5 GB, 3 GB free

    assert check.ok
    assert not old.exists() and active.exists() and (root / "_torrents/x.torrent").exists()


async def test_check_disk_space_protects_unfinished_aria2_files(bot_disk, monkeypatch):
    root = bot_disk.root
    aria2_file = write(root, "Elsewhere.iso", 4 * GB)

    async def unfinished():
        return [{"files": [{"path": str(aria2_file)}]}]

    monkeypatch.setattr(tb, "unfinished_aria2_downloads", unfinished)
    check = await tb.check_disk_space(1 * GB)  # needs 4 GB, 6 GB free
    assert check.ok
    check = await tb.check_disk_space(5 * GB)
    assert not check.ok and aria2_file.exists()


async def test_slot_gate_refuses_job_on_low_disk(bot_disk):
    write(bot_disk.root, "Keep/a.mkv", 8 * GB)
    tb.DISK_GUARD_PROTECTED.append("Keep")
    job = _job()

    assert not await tb.wait_for_download_slot(None, job)

    assert job["status"] == "failed"
    assert job["last_line"] == "Insufficient disk space (needs 3.0 GB, 2.0 GB free)"
    assert bot_disk.finished == ["failed"]


async def test_slot_gate_purges_and_notes_it(bot_disk):
    write(bot_disk.root, "old.mkv", 8 * GB)
    job = _job()
    assert await tb.wait_for_download_slot(None, job)
    assert job["status"] == "starting"
    assert job["disk_note"] == "🧹 Freed 8.0 GB by removing 1 old file"
    text, _ = job_views.job_card(dict(job, status="downloading"))
    assert "Freed 8.0 GB" in text


async def test_slot_gate_skips_disk_for_aria2(bot_disk):
    write(bot_disk.root, "Keep/a.mkv", 9 * GB)
    tb.DISK_GUARD_PROTECTED.append("Keep")
    job = _job()
    assert await tb.wait_for_download_slot(None, job, check_disk=False)
    assert job["status"] == "starting"


async def test_run_with_download_slot_skips_refused_job_and_frees_slot(bot_disk):
    write(bot_disk.root, "Keep/a.mkv", 9 * GB)
    tb.DISK_GUARD_PROTECTED.append("Keep")
    job = _job()
    ran = []

    async def run():
        ran.append(True)

    await tb.run_with_download_slot(None, job, run)
    assert ran == [] and job["status"] == "failed"
    assert not tb.download_slots.holds(1, 1)


async def test_ytdlp_check_runs_once_when_size_known(bot_disk):
    write(bot_disk.root, "Keep/a.mkv", 5 * GB)
    tb.DISK_GUARD_PROTECTED.append("Keep")
    job = _job(status="downloading")
    loop = asyncio.get_running_loop()

    assert await asyncio.to_thread(tb.ytdlp_disk_check, job, 0, 0, loop) is None  # size unknown
    assert "disk_checked_size" not in job
    # 5 GB free: 3 GB still to fetch + 3 GB buffer does not fit.
    reason = await asyncio.to_thread(tb.ytdlp_disk_check, job, 4 * GB, 1 * GB, loop)
    assert reason == "Insufficient disk space (needs 6.0 GB, 5.0 GB free)"
    assert await asyncio.to_thread(tb.ytdlp_disk_check, job, 4 * GB, 1 * GB, loop) is None


async def test_ytdlp_check_passes_when_it_fits(bot_disk):
    job = _job(status="downloading")
    loop = asyncio.get_running_loop()
    assert await asyncio.to_thread(tb.ytdlp_disk_check, job, 2 * GB, 0, loop) is None
    assert job["disk_checked_size"] is True


class FakeAria2:
    def __init__(self):
        self.paused, self.unpaused = [], []

    async def pause(self, gid, force=False):
        self.paused.append(gid)

    async def unpause(self, gid):
        self.unpaused.append(gid)


async def test_aria2_gate_holds_job_then_resume_rechecks(bot_disk, monkeypatch):
    aria2 = FakeAria2()
    monkeypatch.setattr(tb, "aria2_client", aria2)
    write(bot_disk.root, "Keep/a.mkv", 6 * GB)
    tb.DISK_GUARD_PROTECTED.append("Keep")
    job = _job(status="downloading", gid="g1", total_length=5 * GB, files=[])

    assert await tb.aria2_disk_gate(None, job, {"gid": "g1"})

    assert aria2.paused == ["g1"]
    assert job["status"] == "paused"
    assert job["disk_hold"] == "Insufficient disk space (needs 8.0 GB, 4.0 GB free)"
    text, _ = job_views.job_card(job)
    assert "Free some space, then tap Resume" in text
    # Checked once per GID.
    job["status"] = "downloading"
    assert not await tb.aria2_disk_gate(None, job, {"gid": "g1"})

    job["status"] = "paused"
    ok, _ = await tb.resume_job(1)
    assert ok and aria2.unpaused == ["g1"]
    assert "disk_hold" not in job and "sized:g1" not in job["disk_checks"]
    job["status"] = "downloading"
    assert await tb.aria2_disk_gate(None, job, {"gid": "g1"})  # still short: held again


async def test_aria2_gate_counts_preallocated_space(bot_disk, monkeypatch):
    monkeypatch.setattr(tb, "aria2_client", FakeAria2())
    monkeypatch.setattr(disk_guard, "allocated_bytes", lambda paths: 4 * GB)
    job = _job(status="downloading", gid="g1", total_length=10 * GB, files=["/x"])
    # 10 GB free, 6 GB still to allocate + 3 GB buffer fits.
    assert not await tb.aria2_disk_gate(None, job, {"gid": "g1"})
    assert job["status"] == "downloading"


async def test_aria2_gate_waits_for_size_and_download_state(bot_disk):
    job = _job(status="metadata", gid="g1", total_length=0)
    assert not await tb.aria2_disk_gate(None, job, {"gid": "g1"})
    job["status"] = "downloading"
    assert not await tb.aria2_disk_gate(None, job, {"gid": "g1"})
    assert "disk_checks" not in job
