"""Signed file-link helpers for exposing VPS files through the mini-app API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class SignedFileLink:
    """A temporary URL pointing at a local file."""

    url: str
    expires_at: int


class SignedFileLinkService:
    """Create and verify tamper-resistant file links."""

    def __init__(self, secret: str, base_url: str, root: Path) -> None:
        if not secret:
            raise ValueError("file link secret must be configured")
        if not base_url:
            raise ValueError("file link base URL must be configured")
        self.secret = secret.encode()
        self.base_url = base_url.rstrip("/")
        self.root = root.resolve()

    def create_link(self, path: Path, *, ttl_seconds: int = 3600) -> SignedFileLink:
        resolved = path.resolve()
        relative = self._relative_path(resolved)
        expires_at = int(time.time()) + ttl_seconds
        signature = self.sign(relative, expires_at)
        encoded_path = quote(relative.as_posix())
        return SignedFileLink(
            url=f"{self.base_url}/files/{encoded_path}?expires={expires_at}&signature={signature}",
            expires_at=expires_at,
        )

    def verify(self, relative_path: str, expires_at: int, signature: str) -> bool:
        if expires_at < int(time.time()):
            return False
        expected = self.sign(Path(relative_path), expires_at)
        return hmac.compare_digest(expected, signature)

    def sign(self, relative_path: Path, expires_at: int) -> str:
        payload = f"{relative_path.as_posix()}:{expires_at}".encode()
        digest = hmac.new(self.secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def _relative_path(self, path: Path) -> Path:
        try:
            return path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"{path} is outside configured file root {self.root}") from exc
