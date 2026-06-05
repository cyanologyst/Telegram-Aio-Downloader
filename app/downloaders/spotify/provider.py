"""spotDL based Spotify downloader provider."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from app.downloaders.base import BaseDownloader, DownloadRequest
from app.models.download import DownloadArtifact, DownloadResult

SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?"
    r"(track|album|playlist|artist|episode|show)/[A-Za-z0-9]+",
    re.IGNORECASE,
)


def is_spotify_url(text: str) -> bool:
    """Return whether text contains a supported Spotify URL."""

    return bool(SPOTIFY_URL_RE.search(text.strip()))


class SpotifyDownloader(BaseDownloader):
    """Download Spotify links with spotDL into the requested destination."""

    provider_name = "spotify"

    def __init__(self, spotdl_bin: str = "spotdl", ffmpeg_bin: str | None = None) -> None:
        self.spotdl_bin = spotdl_bin
        self.ffmpeg_bin = ffmpeg_bin

    async def can_handle(self, url: str) -> bool:
        return is_spotify_url(url)

    async def download(self, request: DownloadRequest) -> DownloadResult:
        destination = request.destination
        destination.mkdir(parents=True, exist_ok=True)
        before = self._snapshot_files(destination)

        cmd = self._build_command(request.url, destination)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(destination),
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
                progress_callback(line, self._parse_percent(line))

        return_code = await process.wait()
        if return_code != 0:
            tail = "\n".join(output_lines[-8:]) or f"spotDL exited with code {return_code}"
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
        title = self._title_from_artifacts(artifacts) or "Spotify download"
        return DownloadResult(
            provider=self.provider_name,
            title=title,
            artifacts=artifacts,
            metadata={"command": " ".join(cmd)},
        )

    def _build_command(self, url: str, destination: Path) -> list[str]:
        spotdl_bin = shutil.which(self.spotdl_bin) or self.spotdl_bin
        output_template = str(destination / "{artists} - {title}.{output-ext}")
        cmd = [
            spotdl_bin,
            "download",
            url,
            "--output",
            output_template,
            "--overwrite",
            "skip",
            "--scan-for-songs",
            "--print-errors",
        ]
        if self.ffmpeg_bin:
            cmd.extend(["--ffmpeg", self.ffmpeg_bin])
        return cmd

    @staticmethod
    def _snapshot_files(destination: Path) -> set[Path]:
        if not destination.exists():
            return set()
        return {path for path in destination.rglob("*") if path.is_file()}

    @staticmethod
    def _media_type(path: Path) -> str | None:
        ext = path.suffix.lower()
        if ext in {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav"}:
            return "audio"
        if ext in {".lrc", ".m3u", ".m3u8", ".spotdl"}:
            return "metadata"
        return None

    @staticmethod
    def _parse_percent(line: str) -> float | None:
        match = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", line)
        if not match:
            return None
        return max(0.0, min(100.0, float(match.group(1))))

    @staticmethod
    def _title_from_artifacts(artifacts: tuple[DownloadArtifact, ...]) -> str | None:
        audio = [artifact.path.stem for artifact in artifacts if artifact.media_type == "audio"]
        if not audio:
            return None
        if len(audio) == 1:
            return audio[0]
        return f"{len(audio)} Spotify tracks"
