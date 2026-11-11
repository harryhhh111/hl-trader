from __future__ import annotations

import json
import uuid

import psycopg2
import pytest
from fastapi.testclient import TestClient

from hl_app.api import create_app
from hl_app.auth import AuthError, CSRF_COOKIE
from hl_app.db import migrate
from hl_app.services import hyperliquid_accounts as accounts
from hl_app.services import hyperliquid_managed_adjustments as managed
from hl_app.services import hyperliquid_wallets as wallets

from .conftest import PostgresNamespace
from .test_hyperliquid_pairs import ADDRESS_LOWER, VALID

pytestmark = pytest.mark.db
PASSWORD = "correct-horse-battery"
SECOND_ADDRESS = "0x1111111111111111111111111111111111111111"
WALLET_ADDRESS = "0x2222222222222222222222222222222222222222"


@pytest.fixture
def account_app(postgres_namespace: PostgresNamespace, monkeypatch, tmp_path):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    pair_config = tmp_path / "pairs.json"
    pair_config.write_text(json.dumps(VALID), encoding="utf-8")
    monkeypatch.setenv("HL_APP_DATABASE_URL", postgres_namespace.runtime_url)
    monkeypatch.setenv("HL_APP_AUTH_MODE", "account")
    monkeypatch.delenv("HL_APP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("HL_APP_SECURE_COOKIE", "false")
    monkeypatch.setenv("HL_APP_TRUSTED_ORIGINS", "http://127.0.0.1:5173")
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(pair_config))
    with TestClient(create_app()) as client:
        yield client, postgres_namespace


def test_account_register_login_logout_and_password_hash(account_app) -> None:
    client, namespace = account_app
    response = client.post(
        "/api/v1/session/account/register",
        json={"email": "First@Example.com", "password": PASSWORD},
        headers={"Origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 200, response.text
    assert client.get("/api/v1/session").json()["data"] | {"expires_at": None} == {
        "authenticated": True,
        "auth_mode": "account",
        "email": "first@example.com",
        "expires_at": None,
    }
    raw_session = client.cookies.get("hl_session")
    with psycopg2.connect(namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT password_hash FROM auth_accounts")
            stored_hash = cursor.fetchone()[0]
            cursor.execute("SELECT session_sha256 FROM auth_sessions")
            session_hash = cursor.fetchone()[0]
    assert stored_hash.startswith("pbkdf2_sha256$") and PASSWORD not in stored_hash
    assert session_hash != raw_session

    csrf = client.cookies.get(CSRF_COOKIE)
    assert client.post(
        "/api/v1/session/logout",
        headers={"Origin": "http://127.0.0.1:5173", "X-CSRF-Token": csrf},
    ).status_code == 200
    assert client.get("/api/v1/session").json()["data"]["authenticated"] is False
    assert client.post(
        "/api/v1/session/account/login",
        json={"email": "first@example.com", "password": "wrong-password"},
        headers={"Origin": "http://127.0.0.1:5173"},
    ).status_code == 401
    assert client.post(
        "/api/v1/session/account/login",
        json={"email": "first@example.com", "password": PASSWORD},
        headers={"Origin": "http://127.0.0.1:5173"},
    ).status_code == 200


def test_first_account_claims_existing_resources_and_users_are_isolated(account_app) -> None:
    client, namespace = account_app
    with psycopg2.connect(namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO hyperliquid_pair_accounts (pair_id, label, address, is_active) VALUES ('nbis-crwv', '既有账户', %s, true)",
                (ADDRESS_LOWER,),
            )
    service = client.app.state.auth
    first = service.register("first@example.com", PASSWORD)
    first_session = service.authenticate(first.session_token)
    second = service.register("second@example.com", PASSWORD)
    second_session = service.authenticate(second.session_token)
    descriptor = next(item for item in VALID["pairs"] if item["id"] == "nbis-crwv")
    assert accounts.list_accounts(namespace.runtime_url, descriptor, first_session.account_id)["accounts"][0]["address"] == ADDRESS_LOWER
    assert accounts.list_accounts(namespace.runtime_url, descriptor, second_session.account_id)["accounts"] == []

    second_account = accounts.add_account(
        namespace.runtime_url, descriptor, "第二用户", SECOND_ADDRESS, second_session.account_id
    )
    assert second_account["is_active"] is True
    assert accounts.apply_active_accounts(namespace.runtime_url, [descriptor], first_session.account_id)[0]["account_address"] == ADDRESS_LOWER
    assert accounts.apply_active_accounts(namespace.runtime_url, [descriptor], second_session.account_id)[0]["account_address"] == SECOND_ADDRESS

    request_id = str(uuid.uuid4())
    with psycopg2.connect(namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO hyperliquid_api_wallets (
                    network, pair_id, account_address, wallet_address, wallet_version, status,
                    encrypted_private_key, encryption_version, data_key_id, created_by, updated_by, owner_account_id
                ) VALUES ('mainnet', 'nbis-crwv', %s, %s, 1, 'active', 'ENC:v1:test:bm9uY2U=:Y2lwaGVy', 1, 'test', 'first', 'first', %s)
                """,
                (ADDRESS_LOWER, WALLET_ADDRESS, first_session.account_id),
            )
            cursor.execute(
                """
                INSERT INTO hyperliquid_execution_requests (
                    request_id, actor, network, pair_id, account_address, config_revision,
                    target_notional, request_payload_hash, preview_hash, wallet_address,
                    wallet_version, status, result, owner_account_id
                ) VALUES (%s, 'first', 'mainnet', 'nbis-crwv', %s, %s, 2000, %s, %s, %s, 1, 'previewed', '{}', %s)
                """,
                (request_id, ADDRESS_LOWER, "a" * 64, "b" * 64, "c" * 64, WALLET_ADDRESS, first_session.account_id),
            )
    assert wallets.get_wallet(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, first_session.account_id)["configured"] is True
    with pytest.raises(AuthError):
        service.authenticate(second.session_token + "invalid")
    with pytest.raises(wallets.WalletError):
        wallets.get_wallet(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, second_session.account_id)
    assert managed.get_managed_execution(namespace.runtime_url, "nbis-crwv", request_id, first_session.account_id)["request_id"] == request_id
    with pytest.raises(wallets.WalletError) as missing:
        managed.get_managed_execution(namespace.runtime_url, "nbis-crwv", request_id, second_session.account_id)
    assert missing.value.status_code == 404
