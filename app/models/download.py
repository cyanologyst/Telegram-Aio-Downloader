"""Download-related domain models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class DownloadStatus(StrEnum):
    """Lifecycle states shared by downloader providers."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DownloadArtifact:
    """A file produced by a downloader."""

    path: Path
    media_type: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Result returned by a provider after a successful download."""

    provider: str
    title: str
    artifacts: tuple[DownloadArtifact, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
