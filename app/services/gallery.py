"""gallery-dl integration: image and gallery sites nothing else handles.

gallery-dl (https://github.com/mikf/gallery-dl) supports hundreds of sites
(Pixiv, Danbooru and other boorus, Imgur, Reddit, DeviantArt, Kemono, ...).
It runs as a subprocess so a download can be cancelled by terminating it,
and its output (one path per downloaded file) gives live progress.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def gallery_category(url: str) -> str | None:
    """gallery-dl's site name for ``url`` (e.g. "pixiv"), or None if unsupported."""
    try:
        from gallery_dl import extractor
    except ImportError:
        return None
    try:
        found = extractor.find(url)
    except Exception as exc:  # a broken extractor must not break link routing
        logger.debug("gallery-dl could not check %s: %s", url, exc)
        return None
    return getattr(found, "category", None) if found else None


def site_label(category: str) -> str:
    """Human name for a gallery-dl category."""
    return {"directlink": "File link"}.get(category, category.replace("_", " ").title())


def parse_output_line(line: str) -> Path | None:
    """A downloaded file's path from one gallery-dl stdout line.

    Already-downloaded files are printed with a leading "# ".
    """
    line = line.strip()
    if line.startswith("# "):
        line = line[2:]
    if not line or line.startswith(("[", "#")):
        return None
    path = Path(line)
    return path if path.is_absolute() else None


async def download_gallery(
    url: str,
    destination: Path,
    *,
    on_file: Callable[[Path], Any] | None = None,
    set_process: Callable[[asyncio.subprocess.Process], Any] | None = None,
    cookies_file: str | None = None,
    proxy: str | None = None,
) -> list[Path]:
    """Download everything at ``url`` into ``destination``; return the files."""
    await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "gallery_dl", "--destination", str(destination)]
    if cookies_file:
        cmd += ["--cookies", cookies_file]
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(url)

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    if set_process:
        set_process(process)
    stderr_task = asyncio.create_task(process.stderr.read())  # type: ignore[union-attr]
    files: list[Path] = []
    try:
        while True:
            raw = await process.stdout.readline()  # type: ignore[union-attr]
            if not raw:
                break
            path = parse_output_line(raw.decode("utf-8", errors="replace"))
            if path is not None and path.exists():
                files.append(path)
                if on_file:
                    on_file(path)
        await process.wait()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
    if process.returncode != 0 and not files:
        last = stderr.splitlines()[-3:] if stderr else [f"exit code {process.returncode}"]
        raise RuntimeError("gallery-dl failed: " + " ".join(last))
    if not files:
        raise RuntimeError("gallery-dl found nothing to download at this link.")
    return files
