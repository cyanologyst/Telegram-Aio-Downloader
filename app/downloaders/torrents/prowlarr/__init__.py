"""Prowlarr search client. The Telegram UI lives in app.bot.search."""

from app.downloaders.torrents.prowlarr.client import ProwlarrClient, ProwlarrConfigError

__all__ = ["ProwlarrClient", "ProwlarrConfigError"]
