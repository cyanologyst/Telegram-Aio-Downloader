"""Which files automatic uploads keep: the per-user ``auto_upload_types`` setting.

Only automatic uploads (Upload when done, playlists in upload-and-delete mode)
use it. Files that don't match are deleted instead of uploaded; Upload
buttons and the file browser always send what the user picked.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Any, Iterable

VIDEO_EXTS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".3gp",
    ".ts", ".m2ts", ".mts", ".mpg", ".mpeg", ".vob", ".ogv",
})
AUDIO_EXTS = frozenset({
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".alac", ".ape", ".m4b",
})
DOCUMENT_EXTS = frozenset({
    ".pdf", ".epub", ".mobi", ".azw3", ".djvu", ".cbz", ".cbr", ".txt", ".rtf",
    ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp",
})

DEFAULT_AUTO_UPLOAD_TYPES = "all"

# Preset -> extensions it keeps (None keeps everything).
PRESETS: dict[str, frozenset[str] | None] = {
    "all": None,
    "video": VIDEO_EXTS,
    "audio": AUDIO_EXTS,
    "video_audio": VIDEO_EXTS | AUDIO_EXTS,
    "documents": DOCUMENT_EXTS,
}
PRESET_LABELS = {
    "all": "All files",
    "video": "Videos only",
    "audio": "Audio only",
    "video_audio": "Video + audio",
    "documents": "Documents only",
}


def normalize(value: Any) -> str:
    return value if value in PRESETS else DEFAULT_AUTO_UPLOAD_TYPES


def label(value: Any) -> str:
    return PRESET_LABELS[normalize(value)]


def is_video(path: str | PurePath) -> bool:
    return PurePath(path).suffix.lower() in VIDEO_EXTS


def matches(value: Any, path: str | PurePath) -> bool:
    exts = PRESETS[normalize(value)]
    return exts is None or PurePath(path).suffix.lower() in exts


def split(value: Any, paths: Iterable[str]) -> tuple[list[str], list[str]]:
    """(kept, dropped) of ``paths`` under the preset, order preserved."""
    kept: list[str] = []
    dropped: list[str] = []
    for path in paths:
        (kept if matches(value, path) else dropped).append(path)
    return kept, dropped


def filtered_out_note(value: Any, deleted: int) -> str:
    return f"🧹 No matching files for filter {label(value)}, {deleted} file(s) deleted"
