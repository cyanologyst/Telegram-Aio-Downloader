"""Cloud mirror helpers backed by rclone."""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

RCLONE_REMOTE_RE = re.compile(r"^[A-Za-z0-9_.-]+:.+")


@dataclass(frozen=True, slots=True)
class MirrorResult:
    """Result of an rclone mirror operation."""

    source: Path
    destination: str
    output: str


class RcloneMirrorService:
    """Copy completed downloads to any configured rclone remote."""

    def __init__(self, rclone_bin: str = "rclone") -> None:
        self.rclone_bin = rclone_bin

    async def copy_to_remote(
        self,
        source: Path,
        destination: str,
        *,
        extra_args: tuple[str, ...] = (),
    ) -> MirrorResult:
        if not await asyncio.to_thread(source.exists):
            raise FileNotFoundError(source)
        if not RCLONE_REMOTE_RE.match(destination):
            raise ValueError("destination must be an rclone remote path such as 'gdrive:Downloads'")

        command = self._build_copy_command(source, destination, extra_args=extra_args)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await process.communicate()
        output = stdout.decode(errors="replace")
        if process.returncode != 0:
            raise RuntimeError(output.strip() or f"rclone exited with code {process.returncode}")
        return MirrorResult(source=source, destination=destination, output=output)

    def _build_copy_command(
        self,
        source: Path,
        destination: str,
        *,
        extra_args: tuple[str, ...] = (),
    ) -> list[str]:
        rclone_bin = shutil.which(self.rclone_bin) or self.rclone_bin
        return [rclone_bin, "copy", str(source), destination, "--progress", *extra_args]
