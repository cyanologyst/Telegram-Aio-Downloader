from __future__ import annotations

import re
from urllib.parse import urlparse

GENERAL_VIDEO_SITES: dict[str, str] = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "tiktok.com": "TikTok",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "twitter.com": "X / Twitter",
    "x.com": "X / Twitter",
    "vimeo.com": "Vimeo",
    "dailymotion.com": "Dailymotion",
    "twitch.tv": "Twitch",
}

# Adult sites mirrored from Porn_Fetch's supported coverage, plus common yt-dlp
# adult extractors. Keep this list to public, non-DRM video pages only.
ADULT_VIDEO_SITES: dict[str, str] = {
    "pornhub.com": "PornHub",
    "eporner.com": "Eporner",
    "xvideos.com": "XVideos",
    "xhamster.com": "XHamster",
    "xnxx.com": "XNXX",
    "spankbang.com": "SpankBang",
    "missav.com": "MissAV",
    "youporn.com": "YouPorn",
    "porntrex.com": "Porntrex",
    "hqporner.com": "HQPorner",
    "redtube.com": "RedTube",
    "tube8.com": "Tube8",
    "tnaflix.com": "TNAFlix",
    "drtuber.com": "DrTuber",
    "motherless.com": "Motherless",
    "thisvid.com": "ThisVid",
    "rule34video.com": "Rule34Video",
    "txxx.com": "Txxx",
    "sunporno.com": "SunPorno",
    "youjizz.com": "YouJizz",
    "empflix.com": "Empflix",
}

HENTAI_VIDEO_SITES: dict[str, str] = {
    "hstream.moe": "HStream",
    "hentaihaven.com": "HentaiHaven",
    "hentaimama.io": "HentaiMama",
    "hanime.red": "HanimeRed",
}

SUPPORTED_VIDEO_SITES: dict[str, str] = {
    **GENERAL_VIDEO_SITES,
    **ADULT_VIDEO_SITES,
    **HENTAI_VIDEO_SITES,
}


def _host(url: str) -> str:
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    for prefix in ("www.", "m.", "mobile."):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    return host


def _matches_domain(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def is_supported_video_url(url: str) -> bool:
    if not url.lower().startswith(("http://", "https://")):
        return False
    host = _host(url)
    return any(_matches_domain(host, domain) for domain in SUPPORTED_VIDEO_SITES)


def is_adult_video_url(url: str) -> bool:
    if not url.lower().startswith(("http://", "https://")):
        return False
    host = _host(url)
    return any(_matches_domain(host, domain) for domain in ADULT_VIDEO_SITES)


def is_hentai_video_url(url: str) -> bool:
    if not url.lower().startswith(("http://", "https://")):
        return False
    host = _host(url)
    return any(_matches_domain(host, domain) for domain in HENTAI_VIDEO_SITES)


def video_platform_label(url: str) -> str:
    host = _host(url)
    for domain, label in SUPPORTED_VIDEO_SITES.items():
        if _matches_domain(host, domain):
            return label
    return "Video"


def video_platform_slug(url: str) -> str:
    label = video_platform_label(url)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-")
    return slug or "Video"
