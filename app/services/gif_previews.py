"""Short GIF-style previews spread across a video: 10 clips of 6 s by default.

Each clip is a silent H.264 MP4 (what Telegram turns GIFs into anyway). The
clips are joined in timeline order into ONE preview video, sent without a
caption; if that fails they go out as one Telegram album instead (ten clips
fit one album exactly), also without captions.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PREVIEW_COUNT = 10
PREVIEW_SECONDS = 6.0
PREVIEW_WIDTH = 480
PREVIEW_FPS = 12


def preview_plan(
    duration: float | None, count: int = PREVIEW_COUNT, seconds: float = PREVIEW_SECONDS
) -> list[tuple[float, float]]:
    """(start, length) of each clip, evenly spread over the video.

    A video shorter than ``count * seconds`` gets fewer clips, and one shorter
    than a clip gets a single clip of the whole video. Unknown or zero
    duration gives no clips.
    """
    if not duration or duration <= 0:
        return []
    length = min(seconds, duration)
    count = max(1, min(count, int(duration // length)))
    plan = []
    for i in range(count):
        start = duration * (i + 0.5) / count - length / 2
        start = max(0.0, min(start, duration - length))
        plan.append((round(start, 2), round(length, 2)))
    return plan


def timestamp(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def merged_name(source_name: str) -> str:
    """File name of the merged preview: ``Preview - <name>.mp4``."""
    stem = Path(source_name).stem.replace("/", "_")[:100] or "video"
    return f"Preview - {stem}.mp4"


def merge_clips(clips: list[Path], target: Path, *, ffmpeg: str = "ffmpeg") -> Path:
    """Join ``clips`` (in the given order) into ``target`` without re-encoding.

    The clips come from one source with the same settings, so ffmpeg's concat
    demuxer can copy the streams as they are.
    """
    if not clips:
        raise ValueError("No clips to merge.")
    listing = target.parent / f".concat-{target.stem}.txt"
    listing.write_text(
        "".join("file '{}'\n".format(str(Path(c).resolve()).replace("'", "'\\''")) for c in clips),
        encoding="utf-8",
    )
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    finally:
        listing.unlink(missing_ok=True)
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"ffmpeg could not merge the preview clips: {result.stderr.strip()[-300:]}"
        )
    return target


def make_clip(
    source: Path,
    start: float,
    length: float,
    target: Path,
    *,
    ffmpeg: str = "ffmpeg",
    width: int = PREVIEW_WIDTH,
    fps: int = PREVIEW_FPS,
) -> Path:
    """Cut ``length`` seconds from ``start`` and save them as a silent MP4."""
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-ss",
        f"{start:.2f}",
        "-t",
        f"{length:.2f}",
        "-i",
        str(source),
        "-an",
        "-vf",
        f"fps={fps},scale='min({width},iw)':-2:flags=lanczos",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "26",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg could not make a preview clip: {result.stderr.strip()[-300:]}")
    return target
