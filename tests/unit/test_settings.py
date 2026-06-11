from app.config.settings import load_settings


def test_load_settings_parses_user_ids(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "BOT_TOKEN=test-token",
                "API_ID=123",
                "API_HASH=test-hash",
                "ALLOWED_USER_IDS=10, 20, nope",
                "AUTO_CLEANUP_DAYS=14",
                "ARIA2_RPC_HOST=127.0.0.2",
                "ARIA2_RPC_PORT=6801",
                "ARIA2_RPC_SECRET=test-secret",
                "GALLERY_DL_BIN=gallery-dl-custom",
                "RCLONE_BIN=rclone-custom",
                "JDOWNLOADER_API_URL=http://127.0.0.1:3129/",
                "JDOWNLOADER_API_TOKEN=jd-token",
                "FILE_LINK_SECRET=file-secret",
                "FILE_LINK_BASE_URL=https://files.example.test/",
                "RSS_POLL_INTERVAL_SECONDS=120",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.delenv("BOT_TOKEN", raising=False)
    settings = load_settings(env_file)

    assert settings.bot_token == "test-token"
    assert settings.api_id == 123
    assert settings.api_hash == "test-hash"
    assert settings.allowed_user_ids == frozenset({10, 20})
    assert settings.auto_cleanup_days == 14
    assert settings.aria2_rpc_host == "127.0.0.2"
    assert settings.aria2_rpc_port == 6801
    assert settings.aria2_rpc_secret == "test-secret"
    assert settings.gallery_dl_bin == "gallery-dl-custom"
    assert settings.rclone_bin == "rclone-custom"
    assert settings.jdownloader_api_url == "http://127.0.0.1:3129"
    assert settings.jdownloader_api_token == "jd-token"
    assert settings.file_link_secret == "file-secret"
    assert settings.file_link_base_url == "https://files.example.test"
    assert settings.rss_poll_interval_seconds == 120
