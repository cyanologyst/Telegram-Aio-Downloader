from bs4 import BeautifulSoup
from PIL import Image

from app.services.manga import (
    _nhentai_page_count,
    _nhentai_reader_image_url,
    convert_images_to_pdf,
    is_manga_url,
    list_manga_images,
)


def test_manga_url_detection():
    assert is_manga_url("https://mangadex.org/chapter/11111111-1111-1111-1111-111111111111")
    assert is_manga_url("https://example.com/comic/chapter-1")
    assert not is_manga_url("https://open.spotify.com/track/abc")


def test_convert_images_to_pdf_and_remove_sources(tmp_path):
    folder = tmp_path / "Manga" / "Chapter 1"
    folder.mkdir(parents=True)
    for index, color in enumerate(["red", "blue"], start=1):
        Image.new("RGB", (32, 32), color=color).save(folder / f"{index:02d}.jpg")

    output = convert_images_to_pdf(folder, tmp_path, remove_images=True)

    assert output.exists()
    assert output.suffix == ".pdf"
    assert list_manga_images(folder) == []


def test_nhentai_page_count_and_reader_image_detection():
    gallery_html = """
    <li>
        <span class="text">Pages:</span>
        <span class="tags"><a><span class="tag_name pages">107</span></a></span>
    </li>
    """
    page_html = """
    <img src="/images/logo.svg">
    <img src="data:image/svg+xml,%3Csvg%3E" data-src="https://i4.nhentaimg.com/016/hash/107.webp">
    """

    assert _nhentai_page_count(BeautifulSoup(gallery_html, "html.parser"), gallery_html) == 107
    assert (
        _nhentai_reader_image_url(
            BeautifulSoup(page_html, "html.parser"),
            "https://nhentai.xxx/g/560002/107/",
        )
        == "https://i4.nhentaimg.com/016/hash/107.webp"
    )
