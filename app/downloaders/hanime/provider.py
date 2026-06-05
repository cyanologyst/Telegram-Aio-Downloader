"""Hanime.tv downloader based on the HanimeDownloader HLS flow."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin

import httpx
from Cryptodome.Cipher import AES
from Cryptodome.Util.Padding import pad, unpad

from app.downloaders.base import BaseDownloader, DownloadRequest
from app.models.download import DownloadArtifact, DownloadResult

HANIME_URL_RE = re.compile(
    r"https?://hanime\.tv/videos/hentai/([A-Za-z0-9]+(?:-[A-Za-z0-9]+)+)",
    re.IGNORECASE,
)
API_URL = "https://hanime.tv/api/v8"
RESOLUTION_ORDER = {
    "1080p": 0,
    "720p": 1,
    "480p": 2,
    "360p": 3,
}


@dataclass(frozen=True, slots=True)
class HlsSegment:
    uri: str
    key_uri: str | None
    iv: str | None
    sequence: int


@dataclass(frozen=True, slots=True)
class HlsPlaylist:
    segments: tuple[HlsSegment, ...]


def is_hanime_url(text: str) -> bool:
    return bool(HANIME_URL_RE.search(text.strip()))


def hanime_slug_from_url(url: str) -> str:
    match = HANIME_URL_RE.search(url.strip())
    if not match:
        raise ValueError("Invalid hanime.tv URL")
    return match.group(1)


class HanimeDownloader(BaseDownloader):
    """Download a Hanime episode from its HLS manifest."""

    provider_name = "hanime"

    async def can_handle(self, url: str) -> bool:
        return is_hanime_url(url)

    async def download(self, request: DownloadRequest) -> DownloadResult:
        destination = request.destination
        await asyncio.to_thread(destination.mkdir, parents=True, exist_ok=True)
        resolution = str(request.options.get("resolution") or "720p")
        progress_callback = request.options.get("progress_callback")

        slug = hanime_slug_from_url(request.url)
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 Telegram-Aio-Downloader/1.0"},
        ) as client:
            info = await self._fetch_video_info(client, slug)
            streams = self._fetch_streams(info)
            stream = self._select_stream(resolution, streams)
            stream_url = str(stream["url"])
            playlist_response = await client.get(stream_url)
            playlist_response.raise_for_status()
            playlist = parse_hls_playlist(playlist_response.text)
            if not playlist.segments:
                raise RuntimeError("Hanime stream playlist has no segments")

            title = sanitize_filename(self._title_from_info(info) or slug)
            output_path = unique_path(
                destination / f"{title}-{stream.get('height', resolution)}p.mp4"
            )
            key_cache: dict[str, bytes] = {}
            chunks = await self._download_segments(
                client,
                playlist,
                stream_url,
                key_cache,
                progress_callback if callable(progress_callback) else None,
            )

        with output_path.open("wb") as video:
            for chunk in chunks:
                if chunk:
                    video.write(chunk)

        return DownloadResult(
            provider=self.provider_name,
            title=title,
            artifacts=(
                DownloadArtifact(
                    path=output_path,
                    media_type="video",
                    size_bytes=output_path.stat().st_size,
                ),
            ),
            metadata={"resolution": str(stream.get("height") or resolution)},
        )

    async def _fetch_video_info(self, client: httpx.AsyncClient, slug: str) -> dict[str, Any]:
        response = await client.get(f"{API_URL}/video", params={"id": slug})
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    @staticmethod
    def _fetch_streams(info: dict[str, Any]) -> list[dict[str, Any]]:
        return list(info["videos_manifest"]["servers"][0]["streams"])

    @staticmethod
    def _title_from_info(info: dict[str, Any]) -> str | None:
        franchise = info.get("hentai_franchise") or {}
        return franchise.get("title")

    @staticmethod
    def _select_stream(resolution: str, streams: list[dict[str, Any]]) -> dict[str, Any]:
        target_height = resolution.lower().replace("p", "")
        preferred_index = RESOLUTION_ORDER.get(resolution.lower(), 1)

        def is_guest_allowed(stream: dict[str, Any]) -> bool:
            return bool(stream.get("is_guest_allowed", False))

        def matches_height(stream: dict[str, Any]) -> bool:
            return str(stream.get("height")) == target_height

        if 0 <= preferred_index < len(streams):
            stream = streams[preferred_index]
            if is_guest_allowed(stream) and matches_height(stream):
                return stream

        for stream in streams:
            if is_guest_allowed(stream) and matches_height(stream):
                return stream

        for stream in streams:
            if is_guest_allowed(stream):
                return stream

        raise RuntimeError("No guest-accessible Hanime stream is available")

    async def _download_segments(
        self,
        client: httpx.AsyncClient,
        playlist: HlsPlaylist,
        playlist_url: str,
        key_cache: dict[str, bytes],
        progress_callback,
    ) -> list[bytes | None]:
        semaphore = asyncio.Semaphore(8)
        total = len(playlist.segments)
        completed = 0
        results: list[bytes | None] = [None] * total

        async def download_one(index: int, segment: HlsSegment) -> None:
            nonlocal completed
            async with semaphore:
                results[index] = await self._download_segment(
                    client,
                    segment,
                    playlist_url,
                    key_cache,
                )
            completed += 1
            if progress_callback:
                progress_callback(completed, total)

        await asyncio.gather(
            *(download_one(index, segment) for index, segment in enumerate(playlist.segments))
        )
        return results

    async def _download_segment(
        self,
        client: httpx.AsyncClient,
        segment: HlsSegment,
        playlist_url: str,
        key_cache: dict[str, bytes],
    ) -> bytes:
        segment_url = urljoin(playlist_url, segment.uri)
        response = await client.get(segment_url)
        response.raise_for_status()
        data = response.content

        if not segment.key_uri:
            return data

        key_url = urljoin(playlist_url, segment.key_uri)
        if key_url not in key_cache:
            key_response = await client.get(key_url)
            key_response.raise_for_status()
            key_cache[key_url] = key_response.content

        iv = parse_iv(segment.iv, segment.sequence)
        decryptor = AES.new(key_cache[key_url], AES.MODE_CBC, iv=iv)
        if len(data) % decryptor.block_size != 0:
            data = pad(data, decryptor.block_size)
        decrypted = decryptor.decrypt(data)
        try:
            return unpad(decrypted, decryptor.block_size)
        except ValueError:
            return decrypted


def parse_hls_playlist(text: str) -> HlsPlaylist:
    segments: list[HlsSegment] = []
    key_uri: str | None = None
    key_iv: str | None = None
    media_sequence = 0
    next_sequence = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            media_sequence = int(line.split(":", 1)[1])
            next_sequence = media_sequence
            continue
        if line.startswith("#EXT-X-KEY:"):
            attrs = parse_m3u8_attrs(line.split(":", 1)[1])
            key_uri = attrs.get("URI")
            key_iv = attrs.get("IV")
            continue
        if line.startswith("#"):
            continue
        segments.append(HlsSegment(uri=line, key_uri=key_uri, iv=key_iv, sequence=next_sequence))
        next_sequence += 1

    return HlsPlaylist(segments=tuple(segments))


def parse_m3u8_attrs(value: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for part in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", value):
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        attrs[key.strip()] = raw.strip().strip('"')
    return attrs


def parse_iv(value: str | None, sequence: int) -> bytes:
    if value:
        hex_value = value[2:] if value.lower().startswith("0x") else value
        return bytes.fromhex(hex_value.zfill(32))
    return sequence.to_bytes(16, "big")


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or "Hanime")[:160]


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate a unique Hanime output path")
