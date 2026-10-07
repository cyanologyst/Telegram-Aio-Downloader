"""The Pirate Bay crawler (apibay.org). The Telegram UI lives in app.bot.search."""

from app.downloaders.torrents.tpb.crawler import TPBCrawler

__all__ = ["TPBCrawler"]
