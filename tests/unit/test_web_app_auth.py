"""Mini-app API authentication."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from app.services.user_settings import DEFAULT_SETTINGS
from app.web.app import create_web_app

BOT_TOKEN = "123456:TEST-TOKEN"


def build_init_data(user_id: int = 4242, auth_date: int | None = None, **extra) -> str:
    """Produce a correctly signed initData string, the way Telegram does."""
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAF_test",
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
        **extra,
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
def client(tmp_path):
    app = create_web_app(
        str(tmp_path),
        BOT_TOKEN,
        web_app_url="https://example.test",
        require_auth=True,
    )
    app.config["TESTING"] = True
    return app.test_client()


def test_valid_init_data_is_accepted(client):
    resp = client.get("/api/files", headers={"X-Init-Data": build_init_data()})
    assert resp.status_code == 200


def test_missing_init_data_is_rejected(client):
    assert client.get("/api/files").status_code == 401


def test_tampered_payload_is_rejected(client):
    init_data = build_init_data(user_id=1)
    tampered = init_data.replace("Test", "Evil")
    assert client.get("/api/files", headers={"X-Init-Data": tampered}).status_code == 401


def test_stale_signature_is_rejected(client):
    old = build_init_data(auth_date=int(time.time()) - 60 * 60 * 48)
    assert client.get("/api/files", headers={"X-Init-Data": old}).status_code == 401


def test_delete_requires_auth(client, tmp_path):
    victim = tmp_path / "keepme.txt"
    victim.write_text("data", encoding="utf-8")

    resp = client.post("/api/files/delete", json={"paths": ["keepme.txt"]})

    assert resp.status_code == 401
    assert victim.exists()


def test_allowed_user_ids_are_enforced(tmp_path):
    app = create_web_app(
        str(tmp_path), BOT_TOKEN, require_auth=True, allowed_user_ids=frozenset({999})
    )
    app.config["TESTING"] = True
    client = app.test_client()

    assert (
        client.get("/api/files", headers={"X-Init-Data": build_init_data(4242)}).status_code == 403
    )
    assert (
        client.get("/api/files", headers={"X-Init-Data": build_init_data(999)}).status_code == 200
    )


def test_identity_ignores_caller_supplied_ids(client, monkeypatch):
    """A signed request acts as its own user, never one named in the request."""
    seen = []

    def fake_get_user_settings(user_id):
        seen.append(user_id)
        return dict(DEFAULT_SETTINGS)

    monkeypatch.setattr("app.web.app.get_user_settings", fake_get_user_settings)

    resp = client.get(
        "/api/settings?user_id=66666",
        headers={"X-Init-Data": build_init_data(4242)},
    )

    assert resp.status_code == 200
    assert seen == [4242]


def test_auth_can_be_disabled_for_local_use(tmp_path):
    app = create_web_app(str(tmp_path), BOT_TOKEN, require_auth=False)
    app.config["TESTING"] = True
    assert app.test_client().get("/api/files").status_code == 200


def test_cors_is_scoped_to_the_configured_origin(tmp_path):
    app = create_web_app(
        str(tmp_path), BOT_TOKEN, web_app_url="https://example.test", require_auth=False
    )
    app.config["TESTING"] = True
    client = app.test_client()

    allowed = client.get("/api/files", headers={"Origin": "https://example.test"})
    blocked = client.get("/api/files", headers={"Origin": "https://evil.test"})

    assert allowed.headers.get("Access-Control-Allow-Origin") == "https://example.test"
    assert blocked.headers.get("Access-Control-Allow-Origin") is None
