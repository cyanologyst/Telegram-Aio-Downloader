"""Split big videos into parts that each play on their own.

Telegram caps one upload at 2000 MiB (4000 MiB with Premium). Cutting the
bytes of a video leaves every part after the first without a header, so it
can't be played. Instead ffmpeg copies the streams into time segments
(``-c copy -f segment``): no re-encode, and each part is a complete video.

ffmpeg can only cut on keyframes, so a part runs a little past the segment
time and its size varies with the bitrate. Parts aim below the cap and the
caller checks the result (see :func:`oversized_parts`).
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024

# What one upload may weigh, a little under Telegram's 2000/4000 MiB.
DEFAULT_PART_CAP = 1950 * MIB
PREMIUM_PART_CAP = 3900 * MIB
# Parts aim at ~1900 MiB (Premium ~3800) so keyframe overshoot stays under the cap.
PART_TARGET_RATIO = 0.975
MIN_SEGMENT_SECONDS = 10

# Containers whose parts are written as MP4 with the index at the front.
MP4_SUFFIXES = {".mp4", ".m4v", ".mov"}


@dataclass(frozen=True)
class MediaInfo:
    duration: float | None
    size: int


def part_cap(is_premium: bool) -> int:
    return PREMIUM_PART_CAP if is_premium else DEFAULT_PART_CAP


def target_part_bytes(cap: int) -> int:
    return int(cap * PART_TARGET_RATIO)


def probe(path: Path, ffprobe: str = "ffprobe") -> MediaInfo:
    """Duration (seconds, None if unknown) and size of ``path``."""
    size = path.stat().st_size
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        duration = float(json.loads(result.stdout or "{}").get("format", {}).get("duration"))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return MediaInfo(None, size)
    return MediaInfo(duration if duration > 0 else None, size)


def segment_seconds(size: int, duration: float, target: int) -> int:
    """Seconds per part so a part holds about ``target`` bytes at the average bitrate."""
    if size <= 0 or duration <= 0:
        raise ValueError("size and duration must be positive")
    bytes_per_second = size / duration
    return max(MIN_SEGMENT_SECONDS, int(target / bytes_per_second))


def expected_parts(size: int, target: int) -> int:
    return max(1, math.ceil(size / target))


def part_suffix(source: Path) -> str:
    return ".mp4" if source.suffix.lower() in MP4_SUFFIXES else source.suffix.lower() or ".mkv"


def part_pattern(out_dir: Path, suffix: str) -> Path:
    """ffmpeg's printf-style output pattern. A neutral name, so a ``%`` in the
    video's name can't break it; parts are renamed after the split."""
    return out_dir / f"segment_%03d{suffix}"


def build_segment_command(
    ffmpeg: str, source: Path, pattern: Path, seconds: int, suffix: str
) -> list[str]:
    """ffmpeg arguments that copy ``source`` into playable ``seconds``-long parts."""
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(seconds),
        "-segment_start_number", "1",
        "-reset_timestamps", "1",
    ]
    if suffix == ".mp4":
        # The segment muxer ignores a plain -movflags; it has to reach the
        # inner mp4 muxer, or the index ends up at the back of every part.
        cmd += ["-segment_format", "mp4", "-segment_format_options", "movflags=+faststart"]
    cmd.append(str(pattern))
    return cmd


def oversized_parts(parts: list[Path], cap: int) -> list[Path]:
    return [p for p in parts if p.stat().st_size > cap]


def split_video(
    source: Path,
    out_dir: Path,
    cap: int,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    attempts: int = 3,
    run=subprocess.run,
) -> list[Path] | None:
    """Split ``source`` into playable parts under ``cap`` bytes in ``out_dir``.

    Returns the parts in order, or None when it can't be done (unknown
    duration, ffmpeg failed, or a part stayed too big after shrinking the
    segment time); the caller then falls back to a byte split.
    """
    info = probe(source, ffprobe)
    if not info.duration:
        return None
    suffix = part_suffix(source)
    pattern = part_pattern(out_dir, suffix)
    target = target_part_bytes(cap)
    seconds = segment_seconds(info.size, info.duration, target)
    out_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(attempts):
        _clear_parts(out_dir, suffix)
        result = run(
            build_segment_command(ffmpeg, source, pattern, seconds, suffix),
            capture_output=True,
            text=True,
        )
        parts = sorted(out_dir.glob(f"segment_*{suffix}"))
        if result.returncode != 0 or not parts:
            break
        too_big = oversized_parts(parts, cap)
        if not too_big:
            named = [out_dir / f"{source.stem}_part_{i:03d}{suffix}" for i in range(1, len(parts) + 1)]
            for part, name in zip(parts, named):
                part.rename(name)
            return named
        # A burst of high bitrate (or sparse keyframes) overshot: shorten
        # the parts in proportion and try again.
        largest = max(p.stat().st_size for p in too_big)
        shorter = int(seconds * target / largest * 0.95)
        if shorter >= seconds or shorter < MIN_SEGMENT_SECONDS:
            break
        seconds = shorter
    _clear_parts(out_dir, suffix)
    return None


def _clear_parts(out_dir: Path, suffix: str) -> None:
    for part in out_dir.glob(f"segment_*{suffix}"):
        part.unlink(missing_ok=True)
