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


def _fake_spotdl(tmp_path, output: str, make_file: str | None = None):
    script = tmp_path / "fake-spotdl"
    body = f"#!/bin/sh\ncat <<'OUT'\n{output}\nOUT\n"
    if make_file:
        body += f"touch '{make_file}'\n"
    script.write_text(body)
    script.chmod(0o755)
    return str(script)


async def _download(provider, tmp_path):
    from app.downloaders.base import DownloadRequest

    return await provider.download(
        DownloadRequest(url="https://open.spotify.com/track/abc", destination=tmp_path / "out")
    )


async def test_spotdl_exiting_cleanly_without_a_song_is_a_failure(tmp_path):
    import pytest

    output = (
        "Processing query: https://open.spotify.com/track/abc\n"
        "AudioProviderError: YT-DLP download error - https://www.youtube.com/watch?v=x"
    )
    provider = SpotifyDownloader(spotdl_bin=_fake_spotdl(tmp_path, output))

    with pytest.raises(RuntimeError, match="AudioProviderError"):
        await _download(provider, tmp_path)


async def test_spotdl_success_and_already_downloaded(tmp_path):
    song = tmp_path / "out" / "Artist - Song.mp3"
    ok = SpotifyDownloader(spotdl_bin=_fake_spotdl(tmp_path, "Downloaded", make_file=str(song)))
    result = await _download(ok, tmp_path)
    assert result.title == "Artist - Song"

    skipped = SpotifyDownloader(
        spotdl_bin=_fake_spotdl(tmp_path, "Skipping Artist - Song (file already exists)")
    )
    result = await _download(skipped, tmp_path)
    assert result.artifacts == ()


def test_spotdl_gets_cookies_and_proxy(tmp_path):
    provider = SpotifyDownloader(cookie_file="/c.txt", proxy="socks5://p:1")
    command = provider._build_command("https://open.spotify.com/track/abc", tmp_path)
    assert command[-4:] == ["--cookie-file", "/c.txt", "--proxy", "socks5://p:1"]
