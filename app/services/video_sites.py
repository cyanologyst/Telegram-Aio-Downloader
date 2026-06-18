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
    "alphaporno.com": "AlphaPorno",
    "camsoda.com": "CamSoda",
    "drtuber.com": "DrTuber",
    "empflix.com": "Empflix",
    "eporner.com": "Eporner",
    "hellporno.com": "HellPorno",
    "hellporno.net": "HellPorno",
    "hqporner.com": "HQPorner",
    "javhdporn.net": "JavHDPorn",
    "javtiful.com": "Javtiful",
    "lovehomeporn.com": "LoveHomePorn",
    "missav.com": "MissAV",
    "missav.live": "MissAV",
    "missav.ws": "MissAV",
    "missav123.com": "MissAV",
    "motherless.com": "Motherless",
    "njavtv.com": "NJAV",
    "nonktube.com": "NonkTube",
    "pornhub.com": "PornHub",
    "porntop.com": "PornTop",
    "porntrex.com": "Porntrex",
    "redtube.com": "RedTube",
    "rule34video.com": "Rule34Video",
    "sexu.com": "Sexu",
    "spankbang.com": "SpankBang",
    "sunporno.com": "SunPorno",
    "thothub.to": "Thothub",
    "thisvid.com": "ThisVid",
    "tnaflix.com": "TNAFlix",
    "tube8.com": "Tube8",
    "txxx.com": "Txxx",
    "webcamera.pl": "WebCamera.pl",
    "xhamster.com": "XHamster",
    "xnxx.com": "XNXX",
    "xvideos.com": "XVideos",
    "youjizz.com": "YouJizz",
    "youporn.com": "YouPorn",
    "zenporn.com": "ZenPorn",
}

YTDLP_GENERIC_IMPERSONATION_SITES: frozenset[str] = frozenset(
    {
        "javhdporn.net",
    }
)

HENTAI_VIDEO_SITES: dict[str, str] = {
    "hanime.tv": "Hanime",
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


def requires_deno_runtime(url: str) -> bool:
    """Return whether the site's extractor requires Deno."""
    return _matches_domain(_host(url), "hanime.tv")


def requires_ytdlp_generic_impersonation(url: str) -> bool:
    """Return whether yt-dlp's generic extractor should impersonate a browser."""

    if not url.lower().startswith(("http://", "https://")):
        return False
    host = _host(url)
    return any(_matches_domain(host, domain) for domain in YTDLP_GENERIC_IMPERSONATION_SITES)


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
