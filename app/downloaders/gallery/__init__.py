"""gallery-dl downloader integration."""

from app.downloaders.gallery.provider import GalleryDlDownloader, is_gallery_candidate_url

__all__ = ["GalleryDlDownloader", "is_gallery_candidate_url"]
