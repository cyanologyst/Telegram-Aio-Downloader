"""Manga gallery download and PDF conversion helpers."""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from PIL import Image

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
MANGADEX_CHAPTER_RE = re.compile(
    r"https?://(?:www\.)?mangadex\.org/chapter/([0-9a-f-]{36})",
    re.IGNORECASE,
)
MANGA_URL_RE = re.compile(
    r"https?://[^\s]*(manga|comic|chapter|gallery|doujin|nhentai|mangadex|manganato)[^\s]*",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MangaDownloadResult:
    title: str
    folder: Path
    images: tuple[Path, ...]


def is_manga_url(text: str) -> bool:
    """Return whether text looks like a manga/gallery URL."""

    return bool(MANGADEX_CHAPTER_RE.search(text) or MANGA_URL_RE.search(text))


def extract_manga_url(text: str) -> str:
    match = re.search(r"https?://\S+", text.strip())
    return match.group(0).rstrip(").,]") if match else text.strip()


def sanitize_gallery_name(value: str, fallback: str = "Manga") -> str:
    cleaned = unquote(value).strip().replace("\\", " ").replace("/", " ")
    cleaned = re.sub(r"[<>:\"|?*\x00-\x1f]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return (cleaned or fallback)[:120]


def manga_folder_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parts:
        return sanitize_gallery_name(parts[-1], "Manga")
    return sanitize_gallery_name(parsed.netloc, "Manga")


def list_manga_images(folder: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in folder.iterdir()
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ),
        key=_natural_sort_key,
    )


def convert_images_to_pdf(
    folder: Path,
    output_dir: Path,
    *,
    remove_images: bool = False,
    title: str | None = None,
) -> Path:
    """Convert image files in folder to one RGB PDF."""

    images = list_manga_images(folder)
    if not images:
        raise ValueError("No manga images found in this folder")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{sanitize_gallery_name(title or folder.name)}.pdf"
    output_path = unique_path(output_path)

    opened: list[Image.Image] = []
    try:
        for image_path in images:
            with Image.open(image_path) as image:
                ready_image: Image.Image = _pdf_ready_image(image)
                opened.append(ready_image)
        first, rest = opened[0], opened[1:]
        first.save(output_path, "PDF", save_all=True, append_images=rest)
    finally:
        for opened_image in opened:
            opened_image.close()

    if remove_images:
        for image_path in images:
            image_path.unlink(missing_ok=True)

    return output_path


async def download_manga_gallery(url: str, destination_root: Path) -> MangaDownloadResult:
    """Download a supported manga/gallery URL into a dedicated folder."""

    await asyncio.to_thread(destination_root.mkdir, parents=True, exist_ok=True)
    chapter_id = _mangadex_chapter_id(url)
    if chapter_id:
        return await _download_mangadex_chapter(chapter_id, destination_root)
    return await _download_generic_gallery(url, destination_root)


async def _download_mangadex_chapter(
    chapter_id: str,
    destination_root: Path,
) -> MangaDownloadResult:
    api_url = f"https://api.mangadex.org/at-home/server/{chapter_id}"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(api_url)
        response.raise_for_status()
        payload = response.json()

        chapter = payload["chapter"]
        image_names = chapter.get("data") or chapter.get("dataSaver") or []
        if not image_names:
            raise RuntimeError("MangaDex chapter did not return image pages")

        folder = _unique_folder(destination_root / sanitize_gallery_name(chapter_id))
        folder.mkdir(parents=True, exist_ok=True)
        base_url = payload["baseUrl"].rstrip("/")
        hash_value = chapter["hash"]
        images = []
        for index, image_name in enumerate(image_names, start=1):
            image_url = f"{base_url}/data/{hash_value}/{image_name}"
            images.append(await _download_image(client, image_url, folder, index))

    return MangaDownloadResult(title=chapter_id, folder=folder, images=tuple(images))


async def _download_generic_gallery(url: str, destination_root: Path) -> MangaDownloadResult:
    headers = {"User-Agent": "Mozilla/5.0 Telegram-Aio-Downloader/1.0"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        response = await client.get(url)
        response.raise_for_status()
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        title = _page_title(soup) or manga_folder_name_from_url(str(response.url))
        image_urls = _extract_image_urls(soup, str(response.url))
        if not image_urls:
            raise RuntimeError("No gallery images found on this page")

        folder = _unique_folder(destination_root / sanitize_gallery_name(title))
        folder.mkdir(parents=True, exist_ok=True)
        images = []
        for index, image_url in enumerate(image_urls, start=1):
            images.append(await _download_image(client, image_url, folder, index))
            await asyncio.sleep(0.15)

    return MangaDownloadResult(title=title, folder=folder, images=tuple(images))


async def _download_image(
    client: httpx.AsyncClient,
    url: str,
    folder: Path,
    index: int,
) -> Path:
    response = await client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    ext = _image_extension(url, content_type)
    path = folder / f"{index:04d}{ext}"
    path.write_bytes(response.content)
    return path


def _extract_image_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for image in soup.find_all("img"):
        value = (
            image.get("data-src")
            or image.get("data-original")
            or image.get("data-lazy-src")
            or image.get("src")
        )
        if not value:
            srcset = image.get("srcset") or image.get("data-srcset")
            value = _best_srcset_url(srcset if isinstance(srcset, str) else None)
        if not isinstance(value, str) or not value:
            continue
        absolute = urljoin(base_url, value)
        if absolute in seen:
            continue
        if _looks_like_real_image(absolute):
            seen.add(absolute)
            found.append(absolute)
    return found


def _best_srcset_url(srcset: str | None) -> str | None:
    if not srcset:
        return None
    return srcset.split(",")[-1].strip().split(" ")[0]


def _looks_like_real_image(url: str) -> bool:
    path = urlparse(url).path.lower()
    if any(token in path for token in ("logo", "avatar", "icon", "sprite", "blank")):
        return False
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def _image_extension(url: str, content_type: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return ".jpg" if suffix == ".jpeg" else suffix
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    return ".jpg"


def _pdf_ready_image(image: Image.Image) -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        background = Image.new("RGB", image.size, "white")
        background.paste(image.convert("RGBA"), mask=image.convert("RGBA").split()[-1])
        return background
    return image.convert("RGB")


def _page_title(soup: BeautifulSoup) -> str | None:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    heading = soup.find(["h1", "h2"])
    return heading.get_text(" ", strip=True) if heading else None


def _mangadex_chapter_id(url: str) -> str | None:
    match = MANGADEX_CHAPTER_RE.search(url)
    return match.group(1) if match else None


def _unique_folder(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.name} ({index})")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate a unique manga folder")


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate a unique PDF path")


def _natural_sort_key(path: Path) -> list[int | str]:
    parts = re.split(r"(\d+)", path.name.lower())
    return [int(part) if part.isdigit() else part for part in parts]


def remove_manga_folder_if_empty(folder: Path) -> None:
    if folder.exists() and folder.is_dir() and not any(folder.iterdir()):
        shutil.rmtree(folder, ignore_errors=True)
