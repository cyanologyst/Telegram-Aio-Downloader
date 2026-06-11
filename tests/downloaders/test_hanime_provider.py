from app.downloaders.hentai import HanimePluginDownloader, is_hanime_plugin_url


async def test_hanime_provider_detects_plugin_supported_urls():
    provider = HanimePluginDownloader()

    assert await provider.can_handle("https://hanime.tv/videos/hentai/example")
    assert await provider.can_handle("https://hstream.moe/watch/abc")
    assert is_hanime_plugin_url("https://hentaihaven.xxx/watch/example")
    assert not await provider.can_handle("https://youtube.com/watch?v=abc")


def test_hanime_provider_builds_ytdlp_command(tmp_path):
    provider = HanimePluginDownloader(yt_dlp_bin="yt-dlp-custom")

    command = provider._build_command("https://hanime.tv/videos/hentai/example", tmp_path)

    assert command == [
        "yt-dlp-custom",
        "--paths",
        str(tmp_path),
        "--restrict-filenames",
        "--no-playlist",
        "https://hanime.tv/videos/hentai/example",
    ]
