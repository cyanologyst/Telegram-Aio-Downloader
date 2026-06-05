from PIL import Image

from app.services.manga import convert_images_to_pdf, is_manga_url, list_manga_images


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
