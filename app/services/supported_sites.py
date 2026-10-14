"""The in-bot Supported sites list, built from the bot's own link routing.

Only sources the bot actually routes are listed: the video sites it hands
to yt-dlp, gallery-dl's sites, manga, Spotify and torrents/direct links.
"""

from __future__ import annotations

import functools

from app.services.gallery import site_label
from app.services.video_sites import ADULT_VIDEO_SITES, GENERAL_VIDEO_SITES, HENTAI_VIDEO_SITES

PER_PAGE = 30


def _gallery_sites() -> list[str]:
    try:
        from gallery_dl import extractor
    except ImportError:
        return []
    categories = {getattr(cls, "category", "") for cls in extractor.extractors()}
    return [site_label(c) for c in categories if c and c != "directlink"]


def _unique_sorted(names) -> list[str]:
    return sorted(set(names), key=str.lower)


@functools.lru_cache(maxsize=1)
def site_lines() -> tuple[str, ...]:
    """Every list line in order: a "§ Section" header, then its sites."""
    sections = [
        ("Torrents and files", ["Magnet links", ".torrent files", "Direct file links (.zip, .mkv, …)"]),
        ("Videos (yt-dlp)", _unique_sorted(GENERAL_VIDEO_SITES.values())),
        ("Adult videos", _unique_sorted([*ADULT_VIDEO_SITES.values(), *HENTAI_VIDEO_SITES.values()])),
        ("Music", ["Spotify (tracks, albums, playlists, artists, shows)"]),
        ("Manga", ["MangaDex", "nhentai", "E-Hentai", "Manganato", "Other manga/gallery pages"]),
        ("Galleries (gallery-dl)", _unique_sorted(_gallery_sites())),
    ]
    lines: list[str] = []
    for title, names in sections:
        if names:
            lines.append(f"§ {title}")
            lines.extend(names)
    return tuple(lines)


def page_count(total: int, per_page: int = PER_PAGE) -> int:
    return max(1, -(-total // per_page))


def page_of(lines: tuple[str, ...] | list[str], page: int, per_page: int = PER_PAGE) -> tuple[list[str], int, int]:
    """(lines on ``page``, the page actually shown, number of pages); ``page`` is clamped."""
    pages = page_count(len(lines), per_page)
    page = max(0, min(page, pages - 1))
    return list(lines[page * per_page : (page + 1) * per_page]), page, pages
