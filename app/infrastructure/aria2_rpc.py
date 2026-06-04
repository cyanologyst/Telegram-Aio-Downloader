"""Minimal aria2 JSON-RPC daemon client."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


class Aria2RpcError(RuntimeError):
    """Raised when aria2 RPC returns an error response."""


@dataclass(slots=True)
class Aria2DaemonConfig:
    """Configuration for a local aria2 daemon."""

    aria2_bin: str
    download_dir: Path
    rpc_host: str = "127.0.0.1"
    rpc_port: int = 6800
    rpc_secret: str = ""
    session_file: Path | None = None
    secret_file: Path | None = None

    @property
    def rpc_url(self) -> str:
        return f"http://{self.rpc_host}:{self.rpc_port}/jsonrpc"


class Aria2RpcClient:
    """Small async wrapper around aria2's JSON-RPC interface."""

    def __init__(self, config: Aria2DaemonConfig) -> None:
        self.config = config
        self._request_id = 0
        self._process: asyncio.subprocess.Process | None = None
        self._startup_lock = asyncio.Lock()
        self._secret = config.rpc_secret or self._load_or_create_secret()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    def _load_or_create_secret(self) -> str:
        secret_file = self.config.secret_file or self.config.download_dir / ".aria2.rpc-secret"
        try:
            if secret_file.exists():
                existing = secret_file.read_text(encoding="utf-8").strip()
                if existing:
                    return existing

            secret_file.parent.mkdir(parents=True, exist_ok=True)
            secret = secrets.token_urlsafe(24)
            secret_file.write_text(f"{secret}\n", encoding="utf-8")
            secret_file.chmod(0o600)
            return secret
        except OSError:
            return secrets.token_urlsafe(24)

    async def ensure_started(self) -> None:
        """Start aria2 as a local daemon if an RPC endpoint is not already available."""
        async with self._startup_lock:
            if await self.is_ready():
                return

            if shutil.which(self.config.aria2_bin) is None:
                raise RuntimeError(f"aria2 executable not found: {self.config.aria2_bin}")

            self.config.download_dir.mkdir(parents=True, exist_ok=True)
            session_file = self.config.session_file or self.config.download_dir / ".aria2.session"
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.touch(exist_ok=True)

            args = [
                self.config.aria2_bin,
                "--no-conf=true",
                "--enable-rpc=true",
                "--rpc-listen-all=false",
                f"--rpc-listen-port={self.config.rpc_port}",
                f"--rpc-secret={self._secret}",
                "--rpc-allow-origin-all=false",
                "--seed-time=0",
                f"--dir={self.config.download_dir}",
                "--bt-save-metadata=true",
                "--bt-metadata-only=false",
                "--follow-torrent=true",
                "--continue=true",
                "--max-concurrent-downloads=8",
                "--auto-file-renaming=false",
                "--summary-interval=0",
                f"--input-file={session_file}",
                f"--save-session={session_file}",
                "--save-session-interval=30",
            ]

            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.config.download_dir),
            )

            for _ in range(30):
                if await self.is_ready():
                    return
                if self._process.returncode is not None:
                    break
                await asyncio.sleep(0.2)

            stderr = ""
            if self._process.stderr:
                raw = await self._process.stderr.read()
                stderr = raw.decode("utf-8", errors="replace").strip()

            message = "aria2 RPC daemon did not become ready"
            if stderr:
                message = f"{message}: {stderr}"
            message = (
                f"{message}. If port {self.config.rpc_port} is already used by an old "
                "aria2c process, stop it once with `pkill aria2c` and restart the bot."
            )
            raise RuntimeError(message)

    async def is_ready(self) -> bool:
        try:
            await self.call("aria2.getVersion")
            return True
        except Exception:
            return False

    async def call(self, method: str, *params: Any) -> Any:
        self._request_id += 1
        request_params: list[Any] = [f"token:{self._secret}"]
        request_params.extend(params)
        payload = {
            "jsonrpc": "2.0",
            "id": str(self._request_id),
            "method": method,
            "params": request_params,
        }
        data = json.dumps(payload).encode("utf-8")

        def send() -> Any:
            request = urllib.request.Request(
                self.config.rpc_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    body = response.read().decode("utf-8")
            except urllib.error.URLError as exc:
                raise Aria2RpcError(str(exc)) from exc

            decoded = json.loads(body)
            if "error" in decoded:
                error = decoded["error"]
                raise Aria2RpcError(error.get("message", str(error)))
            return decoded.get("result")

        return await asyncio.to_thread(send)

    async def add_uri(self, uri: str, options: dict[str, str] | None = None) -> str:
        await self.ensure_started()
        return cast(str, await self.call("aria2.addUri", [uri], options or {}))

    async def add_torrent(
        self,
        torrent_path: Path,
        options: dict[str, str] | None = None,
    ) -> str:
        await self.ensure_started()
        content = await asyncio.to_thread(torrent_path.read_bytes)
        encoded = base64.b64encode(content).decode("ascii")
        return cast(str, await self.call("aria2.addTorrent", encoded, [], options or {}))

    async def tell_status(self, gid: str) -> dict[str, Any]:
        keys = [
            "gid",
            "status",
            "totalLength",
            "completedLength",
            "downloadSpeed",
            "uploadLength",
            "uploadSpeed",
            "connections",
            "errorCode",
            "errorMessage",
            "followedBy",
            "following",
            "infoHash",
            "numSeeders",
            "seeder",
            "bittorrent",
            "files",
        ]
        return cast(dict[str, Any], await self.call("aria2.tellStatus", gid, keys))

    async def remove(self, gid: str, force: bool = False) -> str:
        await self.ensure_started()
        method = "aria2.forceRemove" if force else "aria2.remove"
        return cast(str, await self.call(method, gid))

    async def pause(self, gid: str, force: bool = False) -> str:
        await self.ensure_started()
        method = "aria2.forcePause" if force else "aria2.pause"
        return cast(str, await self.call(method, gid))

    async def unpause(self, gid: str) -> str:
        await self.ensure_started()
        return cast(str, await self.call("aria2.unpause", gid))

    async def purge_results(self) -> None:
        await self.ensure_started()
        await self.call("aria2.purgeDownloadResult")

    async def shutdown(self) -> None:
        if await self.is_ready():
            await self.call("aria2.shutdown")
