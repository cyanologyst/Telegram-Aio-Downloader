"""Re-encode a video smaller: H.265 (libx265), or H.264 when ffmpeg lacks it.

The output is an MP4 with the index at the front, so Telegram can stream it.
Audio is copied when MP4 can hold it as is, otherwise re-encoded to AAC.
"""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from pathlib import Path

DEFAULT_CRF = 28
DEFAULT_PRESET = "medium"
CRF_CHOICES = [24, 26, 28, 30, 32]
PRESET_CHOICES = ["fast", "medium", "slow"]

# Audio codecs MP4 takes without re-encoding.
MP4_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac", "opus", "flac"}


def normalize_crf(value) -> int:
    try:
        crf = int(value)
    except (TypeError, ValueError):
        return DEFAULT_CRF
    return crf if 0 <= crf <= 51 else DEFAULT_CRF


def normalize_preset(value) -> str:
    return value if value in PRESET_CHOICES else DEFAULT_PRESET


def next_crf(value) -> int:
    crf = normalize_crf(value)
    if crf not in CRF_CHOICES:
        return DEFAULT_CRF
    return CRF_CHOICES[(CRF_CHOICES.index(crf) + 1) % len(CRF_CHOICES)]


def next_preset(value) -> str:
    preset = normalize_preset(value)
    return PRESET_CHOICES[(PRESET_CHOICES.index(preset) + 1) % len(PRESET_CHOICES)]


@lru_cache(maxsize=4)
def pick_encoder(ffmpeg: str = "ffmpeg") -> str:
    """libx265 when this ffmpeg has it, else libx264."""
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return "libx264"
    return "libx265" if " libx265 " in result.stdout else "libx264"


def codec_label(encoder: str) -> str:
    return "H.265" if encoder == "libx265" else "H.264"


def probe_audio_codecs(path: Path, ffprobe: str = "ffprobe") -> list[str] | None:
    """Codec names of the audio streams, or None if ffprobe failed."""
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        streams = json.loads(result.stdout or "{}").get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return [str(s.get("codec_name", "")) for s in streams]


def can_copy_audio(codecs: list[str] | None) -> bool:
    return codecs is not None and all(c in MP4_AUDIO for c in codecs)


def output_path(source: Path, encoder: str) -> Path:
    tag = "x265" if encoder == "libx265" else "x264"
    return source.with_name(f"{source.stem}_{tag}.mp4")


def build_compress_command(
    ffmpeg: str,
    source: Path,
    target: Path,
    *,
    encoder: str,
    crf: int,
    preset: str,
    copy_audio: bool,
) -> list[str]:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-nostats",
        "-progress",
        "pipe:1",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        encoder,
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
    ]
    if encoder == "libx265":
        # hvc1 is the tag Apple players (and Telegram's iOS app) expect.
        cmd += ["-tag:v", "hvc1", "-x265-params", "log-level=error"]
    cmd += ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-movflags", "+faststart", str(target)]
    return cmd
