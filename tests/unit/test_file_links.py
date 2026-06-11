import time

from app.services.file_links import SignedFileLinkService


def test_signed_file_link_round_trip(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    file_path = root / "video.mp4"
    file_path.write_text("data", encoding="utf-8")
    service = SignedFileLinkService("secret", "https://files.example.test", root)

    link = service.create_link(file_path, ttl_seconds=60)
    signature = link.url.rsplit("signature=", maxsplit=1)[1]

    assert link.url.startswith("https://files.example.test/files/video.mp4?")
    assert service.verify("video.mp4", link.expires_at, signature)
    assert not service.verify("video.mp4", int(time.time()) - 1, signature)


def test_signed_file_link_rejects_paths_outside_root(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    service = SignedFileLinkService("secret", "https://files.example.test", root)

    try:
        service.create_link(tmp_path / "outside.mp4")
    except ValueError as exc:
        assert "outside configured file root" in str(exc)
    else:
        raise AssertionError("expected ValueError")
