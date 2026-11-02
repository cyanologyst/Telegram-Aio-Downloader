"""Keep the download disk from filling up.

Before a download starts (and again once its size is known) the bot asks
for ``estimated size + safety buffer`` of free space. If the disk is short,
the oldest finished files under Download/ are deleted first-in-first-out
until enough is free. Files that are still being written (aria2 control
files, yt-dlp ``.part`` files, anything touched in the last minutes), hidden
files and protected paths are never deleted. If purging cannot free enough,
the caller refuses or holds the job with :meth:`SpaceCheck.message`.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

GB = 1024**3
DEFAULT_BUFFER_BYTES = 3 * GB
# Files modified this recently may still be open by a downloader we cannot see.
RECENT_SECONDS = 15 * 60

INCOMPLETE_SUFFIXES = (
    ".aria2",
    ".part",
    ".ytdl",
    ".tmp",
    ".temp",
    ".download",
    ".crdownload",
    ".incomplete",
)
FRAGMENT_RE = re.compile(r"\.part-Frag\d+", re.IGNORECASE)


def gb(size: int | float) -> str:
    return f"{size / GB:.1f} GB"


@dataclass
class SpaceCheck:
    ok: bool
    needed: int  # free bytes required: estimate + buffer
    free: int  # free bytes after any purge
    estimated: int | None = None
    purged: list[Path] = field(default_factory=list)
    reclaimed: int = 0

    def message(self) -> str:
        return f"Insufficient disk space (needs {gb(self.needed)}, {gb(self.free)} free)"

    def purge_note(self) -> str | None:
        if not self.purged:
            return None
        count = len(self.purged)
        return f"Freed {gb(self.reclaimed)} by removing {count} old file{'s' if count != 1 else ''}"


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def required_bytes(estimated: int | None, buffer: int = DEFAULT_BUFFER_BYTES) -> int:
    return max(0, int(estimated or 0)) + max(0, int(buffer))


def allocated_bytes(paths: Iterable[Path]) -> int:
    """Bytes these files already occupy on disk (preallocated space counts)."""
    total = 0
    for path in paths:
        try:
            total += os.stat(path).st_blocks * 512
        except (OSError, AttributeError):
            continue
    return total


def _is_hidden(path: Path, root: Path) -> bool:
    return any(part.startswith(".") for part in path.relative_to(root).parts)


def is_incomplete(path: Path, root: Path) -> bool:
    """True for partial downloads: temp suffixes or an aria2 control file beside it or a parent."""
    name = path.name.lower()
    if name.endswith(INCOMPLETE_SUFFIXES) or FRAGMENT_RE.search(name):
        return True
    current = path
    while current != root and root in current.parents:
        if current.with_name(current.name + ".aria2").exists():
            return True
        current = current.parent
    return False


def is_protected(path: Path, protected: Iterable[Path]) -> bool:
    return any(path == item or item in path.parents for item in protected)


def purge_candidates(
    root: Path,
    protected: Iterable[Path] = (),
    *,
    now: float | None = None,
    min_age: float = RECENT_SECONDS,
) -> list[tuple[float, int, Path]]:
    """Finished files that may be deleted, oldest first: (mtime, size, path)."""
    root = Path(root).resolve()
    protected = [Path(p).resolve() for p in protected]
    now = time.time() if now is None else now
    found: list[tuple[float, int, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.is_symlink() or _is_hidden(path, root):
                continue
            if is_incomplete(path, root) or is_protected(path, protected):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if now - stat.st_mtime < min_age:
                continue
            found.append((stat.st_mtime, stat.st_size, path))
    found.sort(key=lambda item: (item[0], str(item[2])))
    return found


def _prune_empty_parents(path: Path, root: Path, keep: set[Path]) -> None:
    parent = path.parent
    while parent != root and root in parent.parents and parent not in keep:
        try:
            parent.rmdir()  # only succeeds when empty
        except OSError:
            return
        parent = parent.parent


def ensure_space(
    root: Path,
    estimated: int | None = None,
    *,
    buffer: int = DEFAULT_BUFFER_BYTES,
    protected: Iterable[Path] = (),
    auto_purge: bool = True,
    keep_dirs: Iterable[Path] = (),
    free_fn: Callable[[Path], int] = free_bytes,
    now: float | None = None,
    min_age: float = RECENT_SECONDS,
) -> SpaceCheck:
    """Make sure ``estimated + buffer`` bytes are free under ``root``, purging FIFO if allowed."""
    root = Path(root).resolve()
    needed = required_bytes(estimated, buffer)
    free = free_fn(root)
    check = SpaceCheck(ok=free >= needed, needed=needed, free=free, estimated=estimated)
    if check.ok or not auto_purge:
        return check

    keep = {Path(p).resolve() for p in keep_dirs}
    for _, size, path in purge_candidates(root, protected, now=now, min_age=min_age):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("Disk guard could not remove %s: %s", path, exc)
            continue
        logger.info("Disk guard removed %s (%s) to free space", path, gb(size))
        check.purged.append(path)
        check.reclaimed += size
        _prune_empty_parents(path, root, keep)
        free = free_fn(root)
        if free >= needed:
            break
    check.free = free
    check.ok = free >= needed
    return check
