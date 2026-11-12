from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hl_app import wallet_keys
from hl_app.auth import SessionInfo
from hl_app.db import migrate
from hl_app.services import hyperliquid_managed_adjustments as managed
from hl_app.services import hyperliquid_pairs as pairs
from hl_app.services import hyperliquid_wallets as wallets

from .conftest import PostgresNamespace
from .test_hyperliquid_pairs import ADDRESS_LOWER, VALID
from .test_hyperliquid_wallet_crypto import PRIVATE_KEY

pytestmark = pytest.mark.db
WALLET = "0x" + "1" * 40
ACTION_ID = "adjust-0x" + "a" * 32


@pytest.fixture
def ready(postgres_namespace: PostgresNamespace, monkeypatch, tmp_path):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(path))
    monkeypatch.setenv("HL_APP_TRADER_BIN", "/bin/hl")
    monkeypatch.setenv("HL_APP_EXECUTION_STATE_DIR", "/var/lib/hl-trader")
    monkeypatch.setenv("HL_APP_TRADING_ENABLED", "true")
    monkeypatch.setattr(managed.authorization, "verify_authorization", lambda *_args: ADDRESS_LOWER)
    monkeypatch.setattr(managed.wallet_crypto, "decrypt_storage", lambda *_args: PRIVATE_KEY)
    pairs.clear_cache()
    with psycopg2.connect(postgres_namespace.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO hyperliquid_api_wallets (
                    network, pair_id, account_address, wallet_address, wallet_version,
                    status, encrypted_private_key, encryption_version, data_key_id,
                    created_by, updated_by
                ) VALUES ('mainnet', 'nbis-crwv', %s, %s, 1, 'active', 'ENC:v1:test:bm9uY2U=:Y2lwaGVy', 1, 'test', 'test', 'test')
                """,
                (ADDRESS_LOWER, WALLET),
            )
    yield postgres_namespace
    pairs.clear_cache()


def session() -> SessionInfo:
    return SessionInfo("session-hash", "", 0, datetime.now(timezone.utc) + timedelta(hours=1), "harry@example.com")


def report(revision: str, request_id: str, status="succeeded") -> dict:
    return {
        "schema_version": 1,
        "status": status,
        "action_id": ACTION_ID if status != "preview" else None,
        "request_id": request_id,
        "pair_id": "nbis-crwv",
        "network": "mainnet",
        "config_revision": revision,
        "account_address": ADDRESS_LOWER,
        "target_notional": "2000",
        "created_at": "2026-11-04T00:00:00Z",
        "legs": [{"coin": "xyz:NBIS"}, {"coin": "xyz:CRWV"}],
        "diagnostics": [],
    }


def preview(ready, request_id: str) -> dict:
    return managed.preview_managed(
        ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, pairs.load_pairs()[0],
        "2000", request_id, 1, session(),
    )


def execute(ready, request_id: str) -> dict:
    result, _status = managed.execute_managed(
        ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, pairs.load_pairs()[0],
        "2000", "2000", request_id, 1, session(),
    )
    return result


def test_managed_preview_execute_and_duplicate_return_original(ready, monkeypatch) -> None:
    request_id = str(uuid.uuid4())
    monkeypatch.setattr(managed.adjustments, "preview", lambda *_args: report(pairs.load_pairs()[0], request_id, "preview"))
    previewed = preview(ready, request_id)
    assert previewed["managed_status"] == "previewed"
    calls = []

    def invoke(argv, _descriptor, _account, _target, execute, pass_fds=()):
        calls.append((argv, execute, pass_fds))
        return report(pairs.load_pairs()[0], request_id)

    monkeypatch.setattr(managed.adjustments, "_invoke", invoke)
    executed = execute(ready, request_id)
    assert executed["managed_status"] == "succeeded" and executed["action_id"] == ACTION_ID
    assert execute(ready, request_id) == executed
    assert len(calls) == 1 and calls[0][1] is True and len(calls[0][2]) == 1
    assert PRIVATE_KEY not in " ".join(calls[0][0])
    assert managed.get_managed_execution(ready.runtime_url, "nbis-crwv", request_id) == executed


def test_managed_request_id_cannot_change_leg_scope(ready, monkeypatch) -> None:
    request_id = str(uuid.uuid4())
    monkeypatch.setattr(
        managed.adjustments, "preview",
        lambda *args: {**report(pairs.load_pairs()[0], request_id, "preview"), "leg_indices": args[-1]},
    )
    previewed = managed.preview_managed(
        ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, pairs.load_pairs()[0],
        "2000", request_id, 1, session(), [0],
    )
    assert previewed["leg_indices"] == [0]
    with pytest.raises(wallets.WalletError, match="request_id 已用于其他调仓请求"):
        execute(ready, request_id)


def test_unknown_blocks_new_execution_and_wallet_mutation(ready, monkeypatch) -> None:
    request_id = str(uuid.uuid4())
    monkeypatch.setattr(managed.adjustments, "preview", lambda *_args: report(pairs.load_pairs()[0], request_id, "preview"))
    preview(ready, request_id)
    monkeypatch.setattr(
        managed.adjustments, "_invoke",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(managed.adjustments.AdjustmentError(503, "结果未知")),
    )
    unknown = execute(ready, request_id)
    assert unknown["managed_status"] == "unknown"
    with pytest.raises(wallets.WalletError, match="执行中或待核对"):
        wallets.issue_challenge(
            ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, pairs.load_pairs()[0], "delete", session()
        )
    with pytest.raises(wallets.WalletError, match="执行中或待核对"):
        preview(ready, str(uuid.uuid4()))


def test_offline_key_rotation_dry_run_and_apply(ready, monkeypatch, tmp_path) -> None:
    old_key = os.urandom(32)
    new_key = os.urandom(32)
    nonce = os.urandom(12)
    aad = wallet_keys.storage_aad("mainnet", "nbis-crwv", ADDRESS_LOWER, WALLET, 1)
    encrypted = "ENC:v1:old:" + ":".join((
        base64.b64encode(nonce).decode(),
        base64.b64encode(AESGCM(old_key).encrypt(nonce, PRIVATE_KEY.encode(), aad)).decode(),
    ))
    with psycopg2.connect(ready.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE hyperliquid_api_wallets SET encrypted_private_key = %s, data_key_id = 'old'",
                (encrypted,),
            )
    old_path = tmp_path / "old.key"
    new_path = tmp_path / "new.key"
    for path, key in ((old_path, old_key), (new_path, new_key)):
        path.write_bytes(base64.b64encode(key))
        path.chmod(0o600)
    monkeypatch.setenv("HL_APP_MIGRATION_DATABASE_URL", ready.migration_url)
    wallet_keys.rotate(str(old_path), str(new_path), "old", "new", False)
    with psycopg2.connect(ready.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT data_key_id FROM hyperliquid_api_wallets")
            assert cursor.fetchone() == ("old",)
    wallet_keys.rotate(str(old_path), str(new_path), "old", "new", True)
    with psycopg2.connect(ready.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT encrypted_private_key, data_key_id FROM hyperliquid_api_wallets")
            value, key_id = cursor.fetchone()
    parts = value.removeprefix("ENC:v1:").split(":")
    plaintext = AESGCM(new_key).decrypt(
        base64.b64decode(parts[1]), base64.b64decode(parts[2]), aad,
    )
    assert key_id == "new" and plaintext.decode() == PRIVATE_KEY
