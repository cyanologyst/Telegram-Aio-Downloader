"""The Pirate Bay crawler subsystem."""

from app.downloaders.torrents.tpb.crawler import TPBCrawler
from app.downloaders.torrents.tpb.handlers import TPBHandlers

__all__ = ["TPBCrawler", "TPBHandlers"]
