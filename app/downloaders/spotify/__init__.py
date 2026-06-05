"""Spotify download provider powered by spotDL."""

from app.downloaders.spotify.provider import SpotifyDownloader, is_spotify_url

__all__ = ["SpotifyDownloader", "is_spotify_url"]
