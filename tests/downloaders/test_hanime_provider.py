from app.downloaders.hanime.provider import (
    HanimeDownloader,
    hanime_slug_from_url,
    is_hanime_url,
    normalize_stream_url,
    parse_hls_playlist,
    parse_iv,
)


async def test_hanime_provider_detects_supported_urls():
    provider = HanimeDownloader()

    assert await provider.can_handle("https://hanime.tv/videos/hentai/sukebe-elf-tanbouki-1")
    assert is_hanime_url("https://hanime.tv/videos/hentai/youkoso-sukebe-elf-no-mori-e-2")
    assert hanime_slug_from_url("https://hanime.tv/videos/hentai/sukebe-elf-tanbouki-1") == (
        "sukebe-elf-tanbouki-1"
    )
    assert not await provider.can_handle("https://example.com/videos/hentai/test-1")


def test_hanime_stream_selection_prefers_requested_resolution():
    streams = [
        {"height": "1080", "is_guest_allowed": False, "url": "1080.m3u8"},
        {"height": "720", "is_guest_allowed": True, "url": "720.m3u8"},
        {"height": "480", "is_guest_allowed": True, "url": "480.m3u8"},
    ]

    assert HanimeDownloader._select_stream("480p", streams)["url"] == "480.m3u8"
    assert HanimeDownloader._select_stream("1080p", streams)["url"] == "720.m3u8"


def test_hanime_hls_parser_tracks_keys_and_sequences():
    playlist = parse_hls_playlist("""
        #EXTM3U
        #EXT-X-MEDIA-SEQUENCE:7
        #EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x00000000000000000000000000000009
        #EXTINF:4.0,
        seg-1.ts
        #EXTINF:4.0,
        seg-2.ts
        """)

    assert len(playlist.segments) == 2
    assert playlist.segments[0].uri == "seg-1.ts"
    assert playlist.segments[0].key_uri == "key.bin"
    assert playlist.segments[0].sequence == 7
    assert parse_iv(playlist.segments[0].iv, playlist.segments[0].sequence) == bytes.fromhex(
        "00000000000000000000000000000009"
    )
    assert parse_iv(None, 7) == (7).to_bytes(16, "big")


def test_hanime_hls_parser_detects_master_playlist_variants():
    playlist = parse_hls_playlist("""
        #EXTM3U
        #EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=854x480
        480p/index.m3u8
        #EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=1280x720
        720p/index.m3u8
        """)

    assert playlist.segments == ()
    assert playlist.variants == ("480p/index.m3u8", "720p/index.m3u8")


def test_hanime_stream_url_normalization():
    assert normalize_stream_url("//m3u8s.example.com/video.m3u8") == (
        "https://m3u8s.example.com/video.m3u8"
    )
    assert normalize_stream_url("m3u8s.example.com/video.m3u8") == (
        "https://m3u8s.example.com/video.m3u8"
    )
