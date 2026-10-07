"""Turn short silent clips and GIFs into files Telegram shows as GIFs.

Telegram plays an MP4 (H.264, no audio track) sent with sendAnimation as a
looping GIF. Real .gif files are converted too: the MP4 is far smaller.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

BOT_ANIMATION_LIMIT = 50 * 1024 * 1024
GIF_MAX_SECONDS = 60
VIDEO_SUFFIXES = {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".gif"}


@dataclass(frozen=True)
class MediaInfo:
    duration: float | None
    has_audio: bool
    vcodec: str | None
    pix_fmt: str | None
    width: int
    height: int


def probe(path: Path, ffprobe: str = "ffprobe") -> MediaInfo | None:
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return None
    duration = (data.get("format") or {}).get("duration") or video.get("duration")
    try:
        seconds = float(duration) if duration is not None else None
    except ValueError:
        seconds = None
    return MediaInfo(
        duration=seconds,
        has_audio=any(s.get("codec_type") == "audio" for s in streams),
        vcodec=video.get("codec_name"),
        pix_fmt=video.get("pix_fmt"),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
    )


def is_gif_like(path: Path, info: MediaInfo | None) -> bool:
    """A .gif, or a short video with no sound: what people mean by "a GIF"."""
    if path.suffix.lower() == ".gif":
        return True
    if path.suffix.lower() not in VIDEO_SUFFIXES or info is None:
        return False
    return not info.has_audio and info.duration is not None and info.duration <= GIF_MAX_SECONDS


def ready_as_is(path: Path, info: MediaInfo | None) -> bool:
    return (
        info is not None
        and path.suffix.lower() == ".mp4"
        and info.vcodec == "h264"
        and info.pix_fmt == "yuv420p"
        and not info.has_audio
        and (info.duration or 0) <= GIF_MAX_SECONDS
        and path.stat().st_size <= BOT_ANIMATION_LIMIT
    )


def convert(
    source: Path,
    target: Path,
    *,
    ffmpeg: str = "ffmpeg",
    max_side: int = 720,
    max_seconds: int = GIF_MAX_SECONDS,
) -> Path:
    """Silent H.264 MP4, longest side at most ``max_side``, first ``max_seconds``."""
    scale = (
        f"scale='if(gte(iw,ih),min({max_side},iw),-2)':'if(gte(iw,ih),-2,min({max_side},ih))'"
        ":flags=lanczos,pad=ceil(iw/2)*2:ceil(ih/2)*2"
    )
    command = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-t",
        str(max_seconds),
        "-an",
        "-vf",
        scale,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not target.exists():
        raise RuntimeError(f"ffmpeg could not make a GIF: {result.stderr.strip()[-300:]}")
    return target
