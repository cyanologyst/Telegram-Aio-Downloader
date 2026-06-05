from app.downloaders.spotify.provider import SpotifyDownloader, is_spotify_url


async def test_spotify_provider_detects_supported_urls():
    provider = SpotifyDownloader()

    assert await provider.can_handle("https://open.spotify.com/track/abc123")
    assert await provider.can_handle("https://open.spotify.com/album/abc123?si=test")
    assert await provider.can_handle("https://open.spotify.com/intl-de/track/abc123")
    assert await provider.can_handle("https://open.spotify.com/playlist/abc123")
    assert is_spotify_url("listen: https://open.spotify.com/artist/abc123")
    assert not await provider.can_handle("https://youtu.be/abc")
    assert not await provider.can_handle("https://spotify.example.com/track/abc")


def test_spotify_provider_builds_spotdl_command(tmp_path):
    provider = SpotifyDownloader(spotdl_bin="spotdl-custom", ffmpeg_bin="ffmpeg-custom")

    command = provider._build_command("https://open.spotify.com/track/abc123", tmp_path)

    assert command[:3] == ["spotdl-custom", "download", "https://open.spotify.com/track/abc123"]
    assert "--output" in command
    assert str(tmp_path) in command[command.index("--output") + 1]
    assert command[-2:] == ["--ffmpeg", "ffmpeg-custom"]
