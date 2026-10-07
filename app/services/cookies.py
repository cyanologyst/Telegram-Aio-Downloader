"""Browser cookies (cookies.txt) that yt-dlp, spotDL and gallery-dl send to sites.

YouTube refuses many server IPs ("Sign in to confirm you're not a bot") and
some videos are private or age-restricted; cookies from a logged-in browser
get past both. The owner sends the file to the bot, which keeps it here.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

HEADER = "# Netscape HTTP Cookie File"
MAX_BYTES = 2 * 1024 * 1024
HTTPONLY_PREFIX = "#HttpOnly_"


class CookieFileError(ValueError):
    """The upload is not a usable Netscape cookies.txt file."""


@dataclass(frozen=True)
class CookieSummary:
    count: int
    sites: list[str]  # most cookies first, e.g. ["youtube.com", "google.com"]
    expired: int
    saved_at: float | None = None


def _site(domain: str) -> str:
    parts = domain.lstrip(".").lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain


def _entries(text: str) -> list[list[str]]:
    rows = []
    for raw in text.splitlines():
        line = raw[len(HTTPONLY_PREFIX) :] if raw.startswith(HTTPONLY_PREFIX) else raw
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) == 7:
            rows.append(fields)
    return rows


def parse_cookies(data: bytes) -> tuple[str, CookieSummary]:
    """Validate an upload; returns (normalized file text, summary)."""
    if len(data) > MAX_BYTES:
        raise CookieFileError("That file is too big for a cookies.txt (over 2 MB).")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CookieFileError("That file isn't text, so it can't be a cookies.txt.") from exc
    text = text.replace("\r\n", "\n")
    if text.lstrip().startswith(("[", "{")):
        raise CookieFileError(
            "That's a JSON cookie export. Export again in Netscape / cookies.txt format."
        )
    rows = _entries(text)
    if not rows:
        raise CookieFileError(
            "No cookies found. Send a Netscape-format cookies.txt (one cookie per line, "
            "7 tab-separated columns)."
        )
    if not text.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File")):
        text = f"{HEADER}\n{text}"  # yt-dlp refuses files without the header
    return text, summarize(rows)


def summarize(rows: list[list[str]], saved_at: float | None = None) -> CookieSummary:
    now = time.time()
    sites = Counter(_site(row[0]) for row in rows)
    expired = sum(1 for row in rows if row[4].isdigit() and int(row[4]) != 0 and int(row[4]) < now)
    return CookieSummary(
        count=len(rows),
        sites=[site for site, _ in sites.most_common()],
        expired=expired,
        saved_at=saved_at,
    )


def save_cookies(path: Path, text: str) -> None:
    """Write the file readable by the bot's user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def read_summary(path: Path) -> CookieSummary | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        saved_at = path.stat().st_mtime
    except OSError:
        return None
    rows = _entries(text)
    return summarize(rows, saved_at) if rows else None


def has_site(summary: CookieSummary | None, site: str) -> bool:
    return bool(summary and site in summary.sites)
