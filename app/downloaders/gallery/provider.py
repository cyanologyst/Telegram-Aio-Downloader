"""gallery-dl provider.

This module integrates gallery-dl as an external executable instead of copying
code from third-party projects. That keeps licensing boundaries simple while
letting the bot support gallery-dl's broad site catalog.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from app.downloaders.base import BaseDownloader, DownloadRequest
from app.models.download import DownloadArtifact, DownloadResult

GALLERY_URL_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(?:imgur\.com|pixiv\.net|deviantart\.com|gelbooru\.com|danbooru\.donmai\.us|"
    r"twitter\.com|x\.com|instagram\.com|reddit\.com|tumblr\.com|flickr\.com|"
    r"artstation\.com|behance\.net|pinterest\.com)",
    re.IGNORECASE,
)


def is_gallery_candidate_url(text: str) -> bool:
    """Return whether text is likely better handled by gallery-dl."""

    return bool(GALLERY_URL_RE.search(text.strip()))


class GalleryDlDownloader(BaseDownloader):
    """Download image galleries and posts with gallery-dl."""

    provider_name = "gallery-dl"

    def __init__(self, gallery_dl_bin: str = "gallery-dl") -> None:
        self.gallery_dl_bin = gallery_dl_bin

    async def can_handle(self, url: str) -> bool:
        return is_gallery_candidate_url(url)

    async def download(self, request: DownloadRequest) -> DownloadResult:
        destination = request.destination
        destination.mkdir(parents=True, exist_ok=True)
        before = self._snapshot_files(destination)

        command = self._build_command(request.url, destination)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        process_callback = request.options.get("process_callback")
        if callable(process_callback):
            process_callback(process)

        progress_callback = request.options.get("progress_callback")
        output_lines: list[str] = []
        assert process.stdout is not None
        async for raw_line in process.stdout:
            line = raw_line.decode(errors="replace").strip()
            if not line:
                continue
            output_lines.append(line)
            if callable(progress_callback):
                progress_callback(line, None)

        return_code = await process.wait()
        if return_code != 0:
            tail = "\n".join(output_lines[-8:]) or f"gallery-dl exited with code {return_code}"
            raise RuntimeError(tail)

        artifacts = tuple(
            DownloadArtifact(
                path=path,
                media_type=self._media_type(path),
                size_bytes=path.stat().st_size if path.exists() else None,
            )
            for path in sorted(self._snapshot_files(destination) - before)
            if path.is_file()
        )
        return DownloadResult(
            provider=self.provider_name,
            title=self._title_from_url(request.url),
            artifacts=artifacts,
            metadata={"command": " ".join(command)},
        )

    def _build_command(self, url: str, destination: Path) -> list[str]:
        gallery_dl_bin = shutil.which(self.gallery_dl_bin) or self.gallery_dl_bin
        return [
            gallery_dl_bin,
            "--directory",
            str(destination),
            "--no-part",
            "--write-metadata",
            url,
        ]

    @staticmethod
    def _snapshot_files(destination: Path) -> set[Path]:
        if not destination.exists():
            return set()
        return {path for path in destination.rglob("*") if path.is_file()}

    @staticmethod
    def _media_type(path: Path) -> str | None:
        ext = path.suffix.lower()
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}:
            return "image"
        if ext in {".mp4", ".webm", ".mov", ".mkv"}:
            return "video"
        if ext in {".json", ".txt"}:
            return "metadata"
        return None

    @staticmethod
    def _title_from_url(url: str) -> str:
        return url.rstrip("/").rsplit("/", maxsplit=1)[-1] or "gallery-dl download"
