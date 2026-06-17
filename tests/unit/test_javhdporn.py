from app.services.javhdporn import (
    _candidate_video_pages,
    _code_from_url,
    _matching_media_url,
    is_javhdporn_url,
)


def test_javhdporn_url_detection():
    assert is_javhdporn_url("https://www.javhdporn.net/video/pppd-680-decensored/")
    assert is_javhdporn_url("https://javhdporn.net/v1/video/pppd-680/")
    assert not is_javhdporn_url("https://example.com/video/pppd-680/")


def test_code_extraction_from_video_url():
    assert _code_from_url("https://www.javhdporn.net/video/pppd-680-decensored/") == "pppd-680"
    assert _code_from_url("https://www.javhdporn.net/v1/video/pppd00680/") == "pppd-680"


def test_candidate_video_pages_are_filtered_to_same_code():
    html = """
        <a href="https://www.javhdporn.net/v1/video/pppd-680/">same code</a>
        <a href="https://www.javhdporn.net/video/pppd-636-decensored/">related</a>
    """

    assert _candidate_video_pages(
        html,
        "https://www.javhdporn.net/video/pppd-680-decensored/",
        "pppd-680",
    ) == ("https://www.javhdporn.net/v1/video/pppd-680/",)


def test_matching_media_url_prefers_same_code_and_ignores_previews():
    html = """
        https://video.pornfhd.com/v/censored/181936_PPPD-636.mp4
        https://video.pornfhd.com/p/8/c8/pppd-680/preview.png
        https://video.pornfhd.com/v/censored/191101_PPPD-680.mp4
    """

    assert (
        _matching_media_url(html, "pppd-680")
        == "https://video.pornfhd.com/v/censored/191101_PPPD-680.mp4"
    )
