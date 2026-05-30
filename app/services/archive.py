# -*- coding: utf-8 -*-
"""
Zipping utilities module - integrates file archiving functionality for the downloader bot.
Creates a single archive split into fixed-size volumes (WinRAR / 7-Zip style).
"""

from __future__ import annotations

import re
import time
import asyncio
import zipfile
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Callable
from collections import defaultdict

try:
    import pyzipper
except ImportError:
    pyzipper = None

try:
    import py7zr
except ImportError:
    py7zr = None

MAX_ZIP_PART_SIZE = 1 * 1024 * 1024 * 1024  # 1GB

VOLUME_PART_RE = re.compile(r"\.(zip|7z)\.\d{3}$", re.IGNORECASE)

WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

ALREADY_COMPRESSED_EXTS = {
    ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".mp4", ".mkv", ".avi", ".mov", ".mp3", ".ogg", ".wav", ".flac",
    ".pdf", ".apk", ".iso", ".webm",
}

ZIP_LOCKS: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def sanitize_filename(name: str) -> str:
    import re as _re
    name = (name or "").strip().replace("\x00", "")
    name = _re.sub(r'[\\/:\*\?"<>\|]+', "_", name)
    name = _re.sub(r"\s+", " ", name)
    name = name.strip(" .")
    if not name or name in {".", ".."}:
        name = f"file_{int(time.time())}"
    stem = Path(name).stem.upper()
    if stem in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name[:240]


def unique_path(directory: Path, filename: str) -> Path:
    filename = sanitize_filename(filename)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    i = 1
    while True:
        c = directory / f"{stem} ({i}){suffix}"
        if not c.exists():
            return c
        i += 1


def human_size(n: Optional[int]) -> str:
    if n is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f}{u}" if u != "B" else f"{int(f)}B"
        f /= 1024
    return "?"


def is_volume_part(path: Path) -> bool:
    return bool(VOLUME_PART_RE.search(path.name.lower()))


def filter_files_for_archiving(files: List[Path]) -> List[Path]:
    result = []
    for f in files:
        if is_volume_part(f):
            continue
        result.append(f)
    return result


def check_password_support(password: Optional[str], archive_format: str) -> Optional[str]:
    if not password:
        return None
    fmt = (archive_format or "zip").lower()
    if fmt == "7z":
        if py7zr is None:
            return "7z password requires py7zr. Install: pip install py7zr"
        return None
    if pyzipper is None:
        return "ZIP password encryption requires pyzipper. Install: pip install pyzipper"
    return None


def check_archive_format_support(archive_format: str) -> Optional[str]:
    fmt = (archive_format or "zip").lower()
    if fmt == "7z" and py7zr is None:
        return "7z format requires py7zr. Install: pip install py7zr"
    return None


def choose_compression(path: Path, compression_level: int = 5) -> Tuple[int, Optional[int]]:
    ext = path.suffix.lower()
    if ext in ALREADY_COMPRESSED_EXTS:
        return zipfile.ZIP_STORED, None
    level = max(1, min(9, int(compression_level)))
    return zipfile.ZIP_DEFLATED, level


def _unique_arcname(arcname: str, used_names: set) -> str:
    final_arcname = arcname
    k = 1
    while final_arcname in used_names:
        stem = Path(arcname).stem
        suffix = Path(arcname).suffix
        final_arcname = f"{stem} ({k}){suffix}"
        k += 1
    used_names.add(final_arcname)
    return final_arcname


def get_oversized_file_warnings(
    files_to_zip: List[Tuple[int, str, Path, int]],
    max_part_size: int,
) -> List[str]:
    return []


class ZipProgress:
    def __init__(self):
        self.stage = "scanning"
        self.current_file = ""
        self.done_bytes = 0
        self.total_bytes = 0
        self.done_files = 0
        self.total_files = 0
        self.current_part = 0
        self.total_parts = 0
        self.current_part_range = ""
        self.error = None
        self.lock = asyncio.Lock()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "current_file": self.current_file,
            "done_bytes": self.done_bytes,
            "total_bytes": self.total_bytes,
            "done_files": self.done_files,
            "total_files": self.total_files,
            "current_part": self.current_part,
            "total_parts": self.total_parts,
            "current_part_range": self.current_part_range,
            "error": self.error,
        }


def _collect_volume_paths(output_dir: Path, base_name: str, ext: str) -> List[Path]:
    """Collect split volumes (.ext.001, ...) or a single .ext file."""
    ext = ext.lower().lstrip(".")
    numbered = sorted(
        output_dir.glob(f"{base_name}.{ext}.[0-9][0-9][0-9]"),
        key=lambda p: p.name,
    )
    if numbered:
        return numbered
    single = output_dir / f"{base_name}.{ext}"
    if single.exists():
        return [single]
    raise RuntimeError(f"No archive volumes found for {base_name}.{ext}")


def split_file_into_volumes(
    source: Path,
    output_dir: Path,
    base_name: str,
    ext: str,
    volume_size: int,
    progress: Optional[ZipProgress] = None,
    on_volume_created: Optional[Callable[[Path, int, int], Tuple[bool, Optional[str]]]] = None,
) -> List[Path]:
    """
  Split one archive file into fixed-size volumes (7-Zip / WinRAR style).
  Example: archive.zip.001, archive.zip.002, ... (last part may be smaller).
  
  on_volume_created: callback(part_path, current_vol, total_vols) -> (should_delete, error_msg)
    If callback returns (True, None), the part will be deleted after upload.
    """
    volume_size = max(1, int(volume_size))
    ext = ext.lower().lstrip(".")
    total = source.stat().st_size

    if total <= volume_size:
        final = unique_path(output_dir, f"{base_name}.{ext}")
        shutil.move(str(source), str(final))
        if on_volume_created:
            try:
                should_delete, _ = on_volume_created(final, 1, 1)
                if should_delete:
                    final.unlink(missing_ok=True)
                    return []
            except Exception:
                pass
        return [final]

    if progress:
        progress.stage = "splitting"
        progress.total_parts = (total + volume_size - 1) // volume_size

    parts: List[Path] = []
    total_parts = (total + volume_size - 1) // volume_size
    
    with open(source, "rb") as src:
        vol = 1
        while True:
            chunk = src.read(volume_size)
            if not chunk:
                break
            part_name = f"{base_name}.{ext}.{vol:03d}"
            part_path = output_dir / part_name
            if part_path.exists():
                part_path.unlink()
            with open(part_path, "wb") as out:
                out.write(chunk)
            
            # Call callback if provided (could upload and delete the part)
            should_delete = False
            if on_volume_created:
                try:
                    should_delete, _ = on_volume_created(part_path, vol, total_parts)
                except Exception:
                    pass
            
            # Only keep the part if it wasn't deleted by callback
            if not should_delete:
                parts.append(part_path)
            
            if progress:
                progress.current_part = vol
            vol += 1

    source.unlink(missing_ok=True)
    return parts


def _write_single_zip(
    files_to_zip: List[Tuple[int, str, Path, int]],
    target: Path,
    password: Optional[str],
    compression_level: int,
    progress: Optional[ZipProgress],
) -> None:
    zip_cls = zipfile.ZipFile
    zip_kwargs: Dict[str, Any] = {}
    encrypted = bool(password and pyzipper is not None)
    if encrypted:
        zip_cls = pyzipper.AESZipFile
        zip_kwargs["encryption"] = pyzipper.WZ_AES

    used_names: set = set()
    with zip_cls(target, "w", **zip_kwargs) as zf:
        if encrypted:
            zf.setpassword(password.encode("utf-8"))
        if progress:
            progress.stage = "zipping"
        for _, orig_name, fp, size in files_to_zip:
            arcname = sanitize_filename(orig_name or fp.name)
            final_arcname = _unique_arcname(arcname, used_names)
            compression, compresslevel = choose_compression(fp, compression_level)
            if progress:
                progress.current_file = final_arcname
            kwargs: Dict[str, Any] = {"arcname": final_arcname, "compress_type": compression}
            if compression == zipfile.ZIP_DEFLATED and compresslevel is not None:
                kwargs["compresslevel"] = compresslevel
            zf.write(fp, **kwargs)
            if progress:
                progress.done_files += 1
                progress.done_bytes += size


def _create_multivolume_zip(
    files_to_zip: List[Tuple[int, str, Path, int]],
    output_dir: Path,
    base_name: str,
    password: Optional[str],
    compression_level: int,
    volume_size: int,
    progress: Optional[ZipProgress],
    on_volume_created: Optional[Callable[[Path, int, int], Tuple[bool, Optional[str]]]] = None,
) -> List[Path]:
    temp_path = output_dir / f".__tmp_{base_name}_{int(time.time())}.zip"
    try:
        _write_single_zip(files_to_zip, temp_path, password, compression_level, progress)
        return split_file_into_volumes(
            temp_path, output_dir, base_name, "zip", volume_size, progress,
            on_volume_created=on_volume_created
        )
    except Exception:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        raise


def _create_multivolume_7z(
    files_to_zip: List[Tuple[int, str, Path, int]],
    output_dir: Path,
    base_name: str,
    password: Optional[str],
    compression_level: int,
    volume_size: int,
    progress: Optional[ZipProgress],
    on_volume_created: Optional[Callable[[Path, int, int], Tuple[bool, Optional[str]]]] = None,
) -> List[Path]:
    if py7zr is None:
        raise RuntimeError("7z format requires py7zr. Install: pip install py7zr")

    level = max(1, min(9, int(compression_level)))
    filters = [{"id": py7zr.FILTER_LZMA2, "preset": level}]
    target = output_dir / f"{base_name}.7z"
    if target.exists():
        target.unlink()
    for old in output_dir.glob(f"{base_name}.7z.*"):
        old.unlink(missing_ok=True)

    used_names: set = set()
    if progress:
        progress.stage = "zipping"

    with py7zr.SevenZipFile(
        str(target),
        "w",
        password=password or None,
        filters=filters,
        volume_size=volume_size,
    ) as archive:
        for _, orig_name, fp, size in files_to_zip:
            arcname = sanitize_filename(orig_name or fp.name)
            final_arcname = _unique_arcname(arcname, used_names)
            if progress:
                progress.current_file = final_arcname
            archive.write(fp, arcname=final_arcname)
            if progress:
                progress.done_files += 1
                progress.done_bytes += size

    parts = _collect_volume_paths(output_dir, base_name, "7z")
    if progress:
        progress.total_parts = len(parts)
        progress.stage = "done"
    
    # For 7z, call callback for each created volume (after all volumes are created)
    retained_parts = []
    if on_volume_created:
        for idx, part in enumerate(parts, 1):
            try:
                should_delete, _ = on_volume_created(part, idx, len(parts))
                if not should_delete:
                    retained_parts.append(part)
            except Exception:
                retained_parts.append(part)
    else:
        retained_parts = parts
    
    return retained_parts


def make_archive_with_progress(
    files_to_zip: List[Tuple[int, str, Path, int]],
    output_dir: Path,
    zip_name: Optional[str] = None,
    password: Optional[str] = None,
    progress: Optional[ZipProgress] = None,
    max_part_size: int = MAX_ZIP_PART_SIZE,
    archive_format: str = "zip",
    compression_level: int = 5,
    on_volume_created: Optional[Callable[[Path, int, int], Tuple[bool, Optional[str]]]] = None,
) -> List[Path]:
    """
    Create one archive containing all files, split into fixed-size volumes.

    ZIP  -> name.zip.001, name.zip.002, ... (or name.zip if it fits in one part)
    7z   -> name.7z.001, name.7z.002, ... (native py7zr volumes; open .001 in WinRAR)

    max_part_size is the volume size in bytes (from user settings).
    
    on_volume_created: callback(part_path, current_vol, total_vols) -> (should_delete, error_msg)
        Called after each volume is created. If returns (True, None), volume is deleted from disk.
    """
    fmt = (archive_format or "zip").lower()
    fmt_err = check_archive_format_support(fmt)
    if fmt_err:
        raise RuntimeError(fmt_err)

    pwd_err = check_password_support(password, fmt)
    if pwd_err:
        raise RuntimeError(pwd_err)

    if not files_to_zip:
        raise RuntimeError("No files to archive")

    if progress:
        progress.stage = "scanning"
        progress.total_files = len(files_to_zip)
        progress.total_bytes = sum(size for _, _, _, size in files_to_zip)

    if not zip_name:
        zip_name = f"archive_{int(time.time())}"

    base_name = sanitize_filename(zip_name)
    volume_size = max(1, int(max_part_size))

    if fmt == "7z":
        output_paths = _create_multivolume_7z(
            files_to_zip,
            output_dir,
            base_name,
            password,
            compression_level,
            volume_size,
            progress,
            on_volume_created=on_volume_created,
        )
    else:
        output_paths = _create_multivolume_zip(
            files_to_zip,
            output_dir,
            base_name,
            password,
            compression_level,
            volume_size,
            progress,
            on_volume_created=on_volume_created,
        )

    if progress:
        progress.stage = "done"
        progress.current_file = ""
        progress.total_parts = len(output_paths)

    return output_paths


def make_zip_with_progress(
    files_to_zip: List[Tuple[int, str, Path, int]],
    output_dir: Path,
    zip_name: Optional[str] = None,
    password: Optional[str] = None,
    progress: Optional[ZipProgress] = None,
    max_part_size: int = MAX_ZIP_PART_SIZE,
    archive_format: str = "zip",
    compression_level: int = 5,
    on_volume_created: Optional[Callable[[Path, int, int], Tuple[bool, Optional[str]]]] = None,
) -> List[Path]:
    return make_archive_with_progress(
        files_to_zip,
        output_dir,
        zip_name=zip_name,
        password=password,
        progress=progress,
        max_part_size=max_part_size,
        archive_format=archive_format,
        compression_level=compression_level,
        on_volume_created=on_volume_created,
    )


def render_progress_bar(percent: float, width: int = 12) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int((percent / 100.0) * width)
    return "\u2588" * filled + "\u2591" * (width - filled)


def parse_indexes(tokens: List[str]) -> List[int]:
    result = []
    for token in tokens:
        if "-" in token:
            try:
                start, end = token.split("-", 1)
                result.extend(range(int(start.strip()), int(end.strip()) + 1))
            except (ValueError, IndexError):
                pass
        else:
            try:
                result.append(int(token.strip()))
            except ValueError:
                pass
    return sorted(set(result))
