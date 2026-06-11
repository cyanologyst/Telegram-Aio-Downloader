from app.services.cloud_mirror import RcloneMirrorService


def test_rclone_copy_command(tmp_path):
    service = RcloneMirrorService(rclone_bin="rclone-custom")

    command = service._build_copy_command(
        tmp_path,
        "gdrive:Downloads",
        extra_args=("--transfers", "2"),
    )

    assert command == [
        "rclone-custom",
        "copy",
        str(tmp_path),
        "gdrive:Downloads",
        "--progress",
        "--transfers",
        "2",
    ]
