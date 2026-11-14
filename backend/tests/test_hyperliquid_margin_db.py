from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from hl_app.auth import SessionInfo
from hl_app.db import migrate
from hl_app.services import hyperliquid_margin as margin
from hl_app.services import hyperliquid_pairs as pairs

from .conftest import PostgresNamespace
from .test_hyperliquid_pairs import ADDRESS_LOWER, VALID
from .test_hyperliquid_wallet_crypto import PRIVATE_KEY

pytestmark = pytest.mark.db
WALLET = "0x" + "1" * 40
ACTION_ID = "margin-0x" + "a" * 32


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
    monkeypatch.setattr(margin.authorization, "verify_authorization", lambda *_args: ADDRESS_LOWER)
    monkeypatch.setattr(margin.wallet_crypto, "decrypt_storage", lambda *_args: PRIVATE_KEY)
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


def test_managed_margin_is_persistent_and_idempotent(ready, monkeypatch) -> None:
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
            "coin": "xyz:NBIS", "position_size": "10", "position_notional": "100",
            "current_margin": "50", "margin_delta": "30", "target_margin": "80",
            "target_ratio_pct": "80", "expected_signer": WALLET, "nonce": 1, "result": "已追加",
        }

    monkeypatch.setattr(margin.subprocess, "Popen", popen)
    monkeypatch.setattr(
        margin, "communicate_bounded",
        lambda *_args: (json.dumps(result(ready, request_id)), ""),
    )
    args = (
        ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, pairs.load_pairs()[0],
        "xyz:NBIS", "xyz:NBIS 补足逐仓保证金至80%", request_id, 1, session(),
    )
    executed, status = margin.execute_managed(*args)
    assert status == 200 and executed["managed_status"] == "succeeded"
    assert executed["action_id"] == ACTION_ID
    repeated, status = margin.execute_managed(*args)
    assert status == 200 and repeated == executed
    assert len(calls) == 1
    assert calls[0][0][3] == "isolated-margin-execute"
    assert len(calls[0][1]["pass_fds"]) == 1
    assert PRIVATE_KEY not in " ".join(calls[0][0])


@pytest.mark.parametrize("status", ["running", "unknown"])
def test_browser_cannot_bypass_unresolved_managed_execution_on_another_pair(ready, monkeypatch, status):
    request_id = str(uuid.uuid4())
    monkeypatch.setattr(margin.subprocess, "Popen", lambda *_args, **_kwargs: type("Process", (), {"returncode": 0})())
    monkeypatch.setattr(margin, "communicate_bounded", lambda *_args: (json.dumps({
        "schema_version": 1, "status": "unknown", "action_id": ACTION_ID, "request_id": request_id,
        "pair_id": "nbis-crwv", "network": "mainnet", "config_revision": pairs.load_pairs()[0],
        "account_address": ADDRESS_LOWER, "coin": "xyz:NBIS", "target_ratio_pct": "80",
    }), ""))
    revision = pairs.load_pairs()[0]
    result, _code = margin.execute_managed(
        ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, "xyz:NBIS",
        "xyz:NBIS 补足逐仓保证金至80%", request_id, 1, session(),
    )
    assert result["managed_status"] == "unknown"
    with psycopg2.connect(ready.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE hyperliquid_execution_requests SET pair_id = 'other-pair', status = %s WHERE request_id = %s", (status, request_id))
    monkeypatch.setattr(margin, "_invoke", lambda *_args: pytest.fail("must refuse before starting Rust"))
    with pytest.raises(margin.wallets.WalletError, match="禁止追加保证金"):
        margin.prepare_browser(ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, "xyz:NBIS", WALLET)
    with pytest.raises(margin.wallets.WalletError, match="禁止追加保证金"):
        margin.submit_browser(ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, "xyz:NBIS", WALLET, ACTION_ID, "0x" + "1" * 130)
    with pytest.raises(margin.wallets.WalletError, match="禁止追加保证金"):
        margin.execute_managed(
            ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, "xyz:NBIS",
            "xyz:NBIS 补足逐仓保证金至80%", str(uuid.uuid4()), 1, session(),
        )
