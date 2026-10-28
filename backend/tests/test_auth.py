from __future__ import annotations

import base64
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import pytest
from fastapi.testclient import TestClient

from hl_app.api import create_app
from hl_app.auth import AuthError, AuthService, CSRF_COOKIE, initialize, rotate
from hl_app.db import migrate

from .conftest import PostgresNamespace

pytestmark = pytest.mark.db
TOKEN = base64.urlsafe_b64encode(b"a" * 32).decode().rstrip("=")
NEW_TOKEN = base64.urlsafe_b64encode(b"b" * 32).decode().rstrip("=")
OTHER_TOKEN = base64.urlsafe_b64encode(b"c" * 32).decode().rstrip("=")
ORIGIN = "http://127.0.0.1:5173"


@pytest.fixture
def app_client(postgres_namespace: PostgresNamespace, monkeypatch, tmp_path):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    assert initialize(postgres_namespace.migration_url, TOKEN) == 1
    pair_config = tmp_path / "pairs.json"
    pair_config.write_text(json.dumps({"config_version": 1, "pairs": []}), encoding="utf-8")
    monkeypatch.setenv("HL_APP_DATABASE_URL", postgres_namespace.runtime_url)
    monkeypatch.setenv("HL_APP_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("HL_APP_SECURE_COOKIE", "false")
    monkeypatch.setenv("HL_APP_TRUSTED_ORIGINS", ORIGIN)
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(pair_config))
    app = create_app()
    app.state.initiate_shutdown = lambda: None
    with TestClient(app) as client:
        yield client, postgres_namespace


def login(client, token: str = TOKEN):
    return client.post("/api/v1/session/login", json={"token": token}, headers={"Origin": ORIGIN})


def test_all_data_requires_login_and_login_requires_origin(app_client) -> None:
    client, _ = app_client
    assert client.get("/api/v1/hyperliquid/pairs").status_code == 401
    assert client.post("/api/v1/session/login", json={"token": TOKEN}).status_code == 403
    assert client.post(
        "/api/v1/session/login", json={"token": TOKEN}, headers={"Origin": "https://evil.invalid"}
    ).status_code == 403
    assert login(client).status_code == 200
    assert client.get("/api/v1/hyperliquid/pairs").status_code == 200


def test_session_logout_csrf_and_revocation(app_client) -> None:
    client, _ = app_client
    response = login(client)
    assert response.status_code == 200
    assert response.cookies.get("hl_session")
    assert client.get("/api/v1/session").json()["data"]["authenticated"] is True
    assert client.post("/api/v1/session/logout", headers={"Origin": ORIGIN}).status_code == 403
    csrf = client.cookies.get(CSRF_COOKIE)
    response = client.post(
        "/api/v1/session/logout", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    assert client.get("/api/v1/hyperliquid/pairs").status_code == 401


def test_session_database_contains_only_hashes(app_client) -> None:
    client, namespace = app_client
    login(client)
    raw = client.cookies.get("hl_session")
    with psycopg2.connect(namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT session_sha256, auth_epoch FROM auth_sessions")
            stored_hash, epoch = cursor.fetchone()
    assert stored_hash != raw and len(stored_hash) == 64 and epoch == 1


def test_rotation_revokes_sessions_and_rejects_old_configuration(app_client) -> None:
    client, namespace = app_client
    login(client)
    old_cookie = client.cookies.get("hl_session")
    assert rotate(namespace.migration_url, NEW_TOKEN) == 2
    assert rotate(namespace.migration_url, NEW_TOKEN) == 2
    assert client.get("/api/v1/hyperliquid/pairs").status_code == 503
    old_service = AuthService(namespace.runtime_url, TOKEN)
    with pytest.raises(AuthError, match="不一致"):
        old_service.authenticate(old_cookie)
    new_service = AuthService(namespace.runtime_url, NEW_TOKEN)
    with pytest.raises(AuthError) as expired:
        new_service.authenticate(old_cookie)
    assert expired.value.status_code == 401
    credential = new_service.login(NEW_TOKEN)
    assert new_service.authenticate(credential.session_token).auth_epoch == 2
    second_service = AuthService(namespace.runtime_url, NEW_TOKEN)
    assert second_service.authenticate(credential.session_token).auth_epoch == 2


def test_expired_session_is_rejected(app_client) -> None:
    client, namespace = app_client
    login(client)
    with psycopg2.connect(namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE auth_sessions SET expires_at = now() - interval '1 second'")
    assert client.get("/api/v1/hyperliquid/pairs").status_code == 401


def test_login_rate_limit_is_persistent(app_client) -> None:
    _client, namespace = app_client
    service = AuthService(namespace.runtime_url, TOKEN)
    for _ in range(5):
        with pytest.raises(AuthError) as error:
            service.login(OTHER_TOKEN)
        assert error.value.status_code == 401
    restarted = AuthService(namespace.runtime_url, TOKEN)
    with pytest.raises(AuthError) as error:
        restarted.login(OTHER_TOKEN)
    assert error.value.status_code == 429


def test_rotation_and_login_never_issue_valid_old_epoch_session(app_client) -> None:
    _client, namespace = app_client
    old_service = AuthService(namespace.runtime_url, TOKEN)

    def old_login():
        try:
            return old_service.login(TOKEN)
        except AuthError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        login_future = pool.submit(old_login)
        rotate_future = pool.submit(rotate, namespace.migration_url, NEW_TOKEN)
        credential = login_future.result()
        assert rotate_future.result() == 2
    if credential is not None:
        new_service = AuthService(namespace.runtime_url, NEW_TOKEN)
        with pytest.raises(AuthError):
            new_service.authenticate(credential.session_token)


def test_initialize_refuses_overwrite(app_client) -> None:
    _client, namespace = app_client
    with pytest.raises(AuthError, match="拒绝覆盖"):
        initialize(namespace.migration_url, NEW_TOKEN)


@pytest.mark.parametrize(
    ("kind", "bad_value"),
    [("positions", None), ("funding", None), ("diagnostics", 1), ("diagnostics", True), ("diagnostics", {}), ("diagnostics", [42])],
)
def test_e1_e3_require_login_and_degrade_only_bad_block(app_client, monkeypatch, kind, bad_value) -> None:
    from hl_app.services import hyperliquid_pairs as pairs
    from .test_hyperliquid_fixtures import descriptor_for, load_fixture

    snap = load_fixture("v2_snapshot_nbis_crwv.json")
    block_name = "account" if kind == "diagnostics" else kind
    if kind == "positions":
        snap["blocks"]["positions"]["items"][0]["coin"] = []
    elif kind == "funding":
        snap["blocks"]["funding"]["recent_payments"][0]["coin"] = []
    else:
        snap["blocks"]["account"]["diagnostics"] = bad_value
    expected_other = copy.deepcopy({
        name: block for name, block in snap["blocks"].items() if name != block_name
    })
    monkeypatch.setattr(pairs, "load_pairs", lambda: (snap["config_revision"], [descriptor_for(snap)]))
    monkeypatch.setattr(pairs, "_run_dashboard", lambda _args: copy.deepcopy(snap))
    pairs.clear_runtime_cache()
    client, _ = app_client
    path = "/api/v1/hyperliquid/pairs/nbis-crwv/dashboard"
    assert client.get(path).status_code == 401
    assert login(client).status_code == 200
    response = client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    block = body["blocks"][block_name]
    assert block["status"] == "unavailable"
    assert set(pairs.BLOCK_DATA_KEYS[block_name]) <= block.keys()
    assert set(block["source_queries"]) == set(pairs.BLOCK_SOURCE_KEYS[block_name])
    assert {name: body["blocks"][name] for name in expected_other} == expected_other
    pairs.clear_runtime_cache()
