from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from psycopg2 import errors

from hl_app.auth import SessionInfo
from hl_app.db import migrate
from hl_app.services import hyperliquid_pairs as pairs
from hl_app.services import hyperliquid_wallets as wallets
from hl_app.services import hyperliquid_wallet_crypto as wallet_crypto

from .conftest import PostgresNamespace
from .test_hyperliquid_pairs import ADDRESS_LOWER, VALID
from .test_hyperliquid_wallet_crypto import PRIVATE_KEY

pytestmark = pytest.mark.db
FIRST_WALLET = "0x" + "1" * 40
SECOND_WALLET = "0x" + "2" * 40


@pytest.fixture
def ready(postgres_namespace: PostgresNamespace, monkeypatch, tmp_path):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(path))
    monkeypatch.setenv("HL_APP_DATA_ENCRYPTION_KEY_ID", "test")
    monkeypatch.setattr(wallet_crypto, "transport_public_key", lambda: {
        "key_id": "transport", "public_key": "test-public", "algorithm": "RSA-OAEP-3072",
    })
    monkeypatch.setattr(wallet_crypto, "decrypt_transport", lambda payload, expected: PRIVATE_KEY)
    monkeypatch.setattr(wallets.authorization, "verify_authorization", lambda network, account, wallet: account)
    monkeypatch.setattr(wallet_crypto, "encrypt_storage", lambda *_args: "ENC:v1:test:bm9uY2U=:Y2lwaGVy")
    pairs.clear_cache()
    yield postgres_namespace
    pairs.clear_cache()


def session() -> SessionInfo:
    return SessionInfo("session-hash", "", 0, datetime.now(timezone.utc) + timedelta(hours=1), "harry@example.com")


def challenge(ready, operation="save") -> dict:
    return wallets.issue_challenge(
        ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, pairs.load_pairs()[0], operation, session()
    )


def save(ready, challenge_id: str, address=FIRST_WALLET) -> dict:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(wallets, "derive_wallet_address", lambda _key: address)
    try:
        return wallets.save_wallet(
            ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, pairs.load_pairs()[0],
            {"challenge_id": challenge_id}, session(),
        )
    finally:
        monkeypatch.undo()


def test_save_replay_rotate_delete_and_retired_address(ready) -> None:
    first = challenge(ready)
    saved = save(ready, first["challenge_id"])
    assert saved["configured"] is True and saved["wallet_version"] == 1
    with pytest.raises(wallets.WalletError, match="一次性操作凭证") as exc:
        save(ready, first["challenge_id"])
    assert exc.value.status_code == 409

    rotated = save(ready, challenge(ready, "rotate")["challenge_id"], SECOND_WALLET)
    assert rotated["wallet_address"] == SECOND_WALLET and rotated["wallet_version"] == 2
    with pytest.raises(wallets.WalletError, match="已淘汰"):
        save(ready, challenge(ready, "rotate")["challenge_id"], FIRST_WALLET)

    deleted = wallets.delete_wallet(
        ready.runtime_url, "nbis-crwv", ADDRESS_LOWER, pairs.load_pairs()[0],
        challenge(ready, "delete")["challenge_id"], SECOND_WALLET, 2, session(),
    )
    assert deleted["configured"] is False and deleted["wallet_version"] == 3
    with psycopg2.connect(ready.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT encrypted_private_key FROM hyperliquid_api_wallets")
            assert cursor.fetchone() == (None,)
            cursor.execute("SELECT count(*) FROM hyperliquid_api_wallet_retired_addresses")
            assert cursor.fetchone() == (2,)
            cursor.execute("SELECT event_type FROM hyperliquid_api_wallet_events ORDER BY id")
            assert [row[0] for row in cursor.fetchall()] == ["save", "rotate", "delete"]
            with pytest.raises(errors.InsufficientPrivilege):
                cursor.execute("DELETE FROM hyperliquid_api_wallet_events")


def test_version_challenge_cannot_overwrite_newer_wallet(ready) -> None:
    old = challenge(ready)
    save(ready, old["challenge_id"])
    stale = challenge(ready, "rotate")
    save(ready, stale["challenge_id"], SECOND_WALLET)
    with pytest.raises(wallets.WalletError, match="一次性操作凭证|版本"):
        save(ready, stale["challenge_id"], FIRST_WALLET)
    current = wallets.get_wallet(ready.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert current["wallet_address"] == SECOND_WALLET and current["wallet_version"] == 2
