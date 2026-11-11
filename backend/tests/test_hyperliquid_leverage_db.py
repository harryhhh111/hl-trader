from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from hl_app.auth import SessionInfo
from hl_app.db import migrate
from hl_app.services import hyperliquid_leverage as leverage
from hl_app.services import hyperliquid_pairs as pairs

from .conftest import PostgresNamespace
from .test_hyperliquid_pairs import ADDRESS_LOWER, VALID
from .test_hyperliquid_wallet_crypto import PRIVATE_KEY

pytestmark = pytest.mark.db
WALLET = "0x" + "1" * 40
ACTION_ID = "leverage-0x" + "a" * 32


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
    monkeypatch.setattr(leverage.authorization, "verify_authorization", lambda *_args: ADDRESS_LOWER)
    monkeypatch.setattr(leverage.wallet_crypto, "decrypt_storage", lambda *_args: PRIVATE_KEY)
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


def test_managed_leverage_is_persistent_and_idempotent(ready, monkeypatch) -> None:
    request_id = str(uuid.uuid4())
    calls = []

    class Process:
        returncode = 0

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return Process()

    def result(ready, request_id):
        return {
            "schema_version": 1, "status": "succeeded", "action_id": ACTION_ID,
            "request_id": request_id, "pair_id": "nbis-crwv", "network": "mainnet",
            "config_revision": pairs.load_pairs()[0], "account_address": ADDRESS_LOWER,
            "coin": "xyz:NBIS", "current_leverage": 3, "current_margin_mode": "cross",
            "leverage": 2, "margin_mode": "isolated", "max_leverage": 5,
            "expected_signer": WALLET, "nonce": 1, "result": "已设置",
        }

    monkeypatch.setattr(leverage.subprocess, "Popen", popen)
    monkeypatch.setattr(
        leverage, "communicate_bounded",
        lambda *_args: (json.dumps(result(ready, request_id)), ""),
    )
    args = (
        ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, pairs.load_pairs()[0],
        "xyz:NBIS", 2, "isolated", "xyz:NBIS isolated 2x", request_id, 1, session(),
    )
    executed, status = leverage.execute_managed(*args)
    assert status == 200 and executed["managed_status"] == "succeeded"
    assert executed["action_id"] == ACTION_ID
    repeated, status = leverage.execute_managed(*args)
    assert status == 200 and repeated == executed
    assert len(calls) == 1
    assert calls[0][0][3] == "leverage-execute"
    assert len(calls[0][1]["pass_fds"]) == 1
    assert PRIVATE_KEY not in " ".join(calls[0][0])
