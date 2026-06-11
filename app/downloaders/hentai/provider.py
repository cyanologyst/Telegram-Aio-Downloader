"""yt-dlp provider configured for hanime-plugin supported sites."""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from app.downloaders.base import BaseDownloader, DownloadRequest
from app.models.download import DownloadArtifact, DownloadResult

HANIME_PLUGIN_URL_RE = re.compile(
    r"https?://(?:www\.)?"
    r"(?:hanime\.tv|hstream\.moe|hentaihaven\.(?:com|xxx)|ohentai\.org|oppai\.stream|"
    r"hanime\.red|hentaimama\.io)",
    re.IGNORECASE,
)


def is_hanime_plugin_url(text: str) -> bool:
    """Return whether text targets a site covered by hanime-plugin."""

    return bool(HANIME_PLUGIN_URL_RE.search(text.strip()))


class HanimePluginDownloader(BaseDownloader):
    """Download hentai video pages through yt-dlp and hanime-plugin."""

    provider_name = "hanime-plugin"

    def __init__(self, yt_dlp_bin: str = "yt-dlp") -> None:
        self.yt_dlp_bin = yt_dlp_bin

    async def can_handle(self, url: str) -> bool:
        return is_hanime_plugin_url(url)

    async def download(self, request: DownloadRequest) -> DownloadResult:
        destination = request.destination
        destination.mkdir(parents=True, exist_ok=True)
        before = self._snapshot_files(destination)

        command = self._build_command(request.url, destination)
        process = await asyncio.create_subprocess_exec(
            *command,
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
            tail = "\n".join(output_lines[-8:]) or f"yt-dlp exited with code {return_code}"
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
            title=self._title_from_artifacts(artifacts) or "hanime-plugin download",
            artifacts=artifacts,
            metadata={"command": " ".join(command)},
        )

    def _build_command(self, url: str, destination: Path) -> list[str]:
        yt_dlp_bin = shutil.which(self.yt_dlp_bin) or self.yt_dlp_bin
        return [
            yt_dlp_bin,
            "--paths",
            str(destination),
            "--restrict-filenames",
            "--no-playlist",
            url,
        ]

    @staticmethod
    def _snapshot_files(destination: Path) -> set[Path]:
        if not destination.exists():
            return set()
        return {path for path in destination.rglob("*") if path.is_file()}

    @staticmethod
    def _media_type(path: Path) -> str | None:
        if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}:
            return "video"
        if path.suffix.lower() in {".vtt", ".srt", ".json", ".info.json"}:
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
        videos = [artifact.path.stem for artifact in artifacts if artifact.media_type == "video"]
        if len(videos) == 1:
            return videos[0]
        if videos:
            return f"{len(videos)} videos"
        return None
