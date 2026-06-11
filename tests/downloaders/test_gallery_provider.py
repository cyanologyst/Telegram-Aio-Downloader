from app.downloaders.gallery import GalleryDlDownloader, is_gallery_candidate_url


async def test_gallery_provider_detects_supported_urls():
    provider = GalleryDlDownloader()

    assert await provider.can_handle("https://www.pixiv.net/artworks/123")
    assert await provider.can_handle("https://imgur.com/a/abc")
    assert is_gallery_candidate_url("https://reddit.com/r/pics/comments/abc/example")
    assert not await provider.can_handle("https://example.com/file.zip")


def test_gallery_provider_builds_command(tmp_path):
    provider = GalleryDlDownloader(gallery_dl_bin="gallery-dl-custom")

    command = provider._build_command("https://imgur.com/a/abc", tmp_path)

    assert command == [
        "gallery-dl-custom",
        "--directory",
        str(tmp_path),
        "--no-part",
        "--write-metadata",
        "https://imgur.com/a/abc",
    ]
