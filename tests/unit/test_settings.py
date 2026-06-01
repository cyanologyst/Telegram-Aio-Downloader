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
