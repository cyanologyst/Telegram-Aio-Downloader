"""Plan and apply "Organize videos": lift videos out of sub-folders.

Torrents usually arrive as ``Folder/Movie.mkv`` plus samples, .nfo files and
subtitles. Organizing moves each video (and subtitles named after it) up into
the folder being organized. It is deliberately conservative:

* it only touches the chosen folder's sub-folders, never anything above it;
* sub-folders with a download still in progress are skipped entirely
  (aria2 ``.aria2`` control files, yt-dlp ``.part``/``.ytdl`` files, or a
  name the caller reports as busy);
* when organizing the Download root, the bot's own library folders
  (Telegram, Spotify, Manga, ...) are left alone;
* left-over files are only deleted when the caller asks for it, and the
  preview reports how many there are and how much space they use.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"}
SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx"}
BUSY_SUFFIXES = {".aria2", ".part", ".ytdl"}


@dataclass
class OrganizePlan:
    folder: Path
    moves: list[tuple[Path, Path]] = field(default_factory=list)
    folders: list[Path] = field(default_factory=list)
    skipped_busy: list[str] = field(default_factory=list)
    leftover_files: int = 0
    leftover_bytes: int = 0

    @property
    def video_count(self) -> int:
        return sum(1 for src, _ in self.moves if src.suffix.lower() in VIDEO_EXTS)


@dataclass
class OrganizeResult:
    moved: int = 0
    removed_folders: int = 0
    kept_folders: int = 0
    errors: list[str] = field(default_factory=list)


def _is_busy(child: Path, busy_names: set[str]) -> bool:
    if child.name in busy_names:
        return True
    if child.with_name(child.name + ".aria2").exists():
        return True
    return any(p.suffix.lower() in BUSY_SUFFIXES for p in child.rglob("*") if p.is_file())


def _unique_target(dst: Path, taken: set[Path]) -> Path:
    candidate = dst
    i = 1
    while candidate.exists() or candidate in taken:
        candidate = dst.with_name(f"{dst.stem}_{i}{dst.suffix}")
        i += 1
    return candidate


def plan_organize(
    folder: Path,
    *,
    protected_names: Iterable[str] = (),
    busy_names: Iterable[str] = (),
) -> OrganizePlan:
    """Work out what organizing ``folder`` would do, without touching disk."""
    folder = folder.resolve()
    protected = set(protected_names)
    busy = set(busy_names)
    plan = OrganizePlan(folder=folder)
    taken: set[Path] = set()

    for child in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.is_symlink() or child.name in protected:
            continue
        files = [p for p in child.rglob("*") if p.is_file() and not p.is_symlink()]
        videos = [p for p in files if p.suffix.lower() in VIDEO_EXTS]
        if not videos:
            continue
        if _is_busy(child, busy):
            plan.skipped_busy.append(child.name)
            continue

        moving: set[Path] = set()
        for video in sorted(videos):
            target = _unique_target(folder / video.name, taken)
            taken.add(target)
            plan.moves.append((video, target))
            moving.add(video)
            # Prefix match, not glob(): torrent names often contain "[...]".
            for sub in sorted(video.parent.iterdir()):
                if (
                    sub.is_file()
                    and sub.name.startswith(video.stem)
                    and sub.suffix.lower() in SUBTITLE_EXTS
                    and sub not in moving
                ):
                    sub_target = _unique_target(
                        folder / (target.stem + sub.name[len(video.stem) :]), taken
                    )
                    taken.add(sub_target)
                    plan.moves.append((sub, sub_target))
                    moving.add(sub)

        plan.folders.append(child)
        for leftover in (p for p in files if p not in moving):
            plan.leftover_files += 1
            plan.leftover_bytes += leftover.stat().st_size

    return plan


def apply_organize(plan: OrganizePlan, *, delete_leftovers: bool) -> OrganizeResult:
    """Execute a plan. Folders are only removed when empty or when asked to."""
    result = OrganizeResult()
    for src, dst in plan.moves:
        try:
            if dst.exists():
                raise FileExistsError(f"target already exists: {dst.name}")
            if not src.exists():
                raise FileNotFoundError("file disappeared")
            shutil.move(str(src), str(dst))
            if src.suffix.lower() in VIDEO_EXTS:
                result.moved += 1
        except OSError as exc:
            result.errors.append(f"{src.name}: {exc}")

    for child in plan.folders:
        if not child.exists():
            continue
        try:
            if delete_leftovers:
                shutil.rmtree(child)
                result.removed_folders += 1
                continue
            # Remove only directories left completely empty, deepest first.
            for sub in sorted(
                (p for p in child.rglob("*") if p.is_dir()),
                key=lambda p: len(p.parts),
                reverse=True,
            ):
                if not any(sub.iterdir()):
                    sub.rmdir()
            if any(child.iterdir()):
                result.kept_folders += 1
            else:
                child.rmdir()
                result.removed_folders += 1
        except OSError as exc:
            result.errors.append(f"{child.name}: {exc}")
    return result
