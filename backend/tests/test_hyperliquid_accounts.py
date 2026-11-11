from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hl_app.auth import require_session
from hl_app.routes import hyperliquid
from hl_app.services import hyperliquid_accounts as accounts
from hl_app.services import hyperliquid_pairs as pairs

from .test_hyperliquid_pairs import ADDRESS, ADDRESS_LOWER, VALID, write_config

SECOND = "0x1111111111111111111111111111111111111111"


@pytest.fixture
def client(monkeypatch, tmp_path):
    config_file = tmp_path / "pairs.json"
    config_file.write_text(__import__("json").dumps(VALID), encoding="utf-8")
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(config_file))
    pairs.clear_cache()
    app = FastAPI()
    app.state.settings = SimpleNamespace(database_url="postgresql://runtime")
    app.include_router(hyperliquid.router, prefix="/api/v1")
    app.dependency_overrides[require_session] = lambda: None
    yield TestClient(app)
    pairs.clear_cache()


def test_account_endpoints_use_configured_pair(client, monkeypatch) -> None:
    monkeypatch.setattr(accounts, "list_accounts", lambda url, descriptor, owner=None: {
        "pair_id": descriptor["id"],
        "accounts": [{"id": None, "label": "默认账户", "address": descriptor["account_address"], "is_active": True}],
    })
    response = client.get("/api/v1/hyperliquid/pairs/nbis-crwv/accounts")
    assert response.status_code == 200
    assert response.json()["data"]["accounts"][0]["address"] == ADDRESS_LOWER

    calls = []

    def add(url, descriptor, label, address, owner=None):
        calls.append((url, descriptor["id"], label, address))
        return {"id": 2, "label": label, "address": address, "is_active": True}

    monkeypatch.setattr(accounts, "add_account", add)
    response = client.post("/api/v1/hyperliquid/pairs/nbis-crwv/accounts", json={"label": "账户二", "address": SECOND})
    assert response.status_code == 201
    assert calls == [("postgresql://runtime", "nbis-crwv", "账户二", SECOND)]

    monkeypatch.setattr(accounts, "activate_account", lambda url, descriptor, account_id, owner=None: {
        "id": account_id, "label": "账户二", "address": SECOND, "is_active": True,
    })
    response = client.put("/api/v1/hyperliquid/pairs/nbis-crwv/accounts/2/activate")
    assert response.status_code == 200
    assert response.json()["data"]["is_active"] is True


def test_unknown_pair_and_account_errors(client, monkeypatch) -> None:
    assert client.get("/api/v1/hyperliquid/pairs/missing/accounts").status_code == 404

    def duplicate(*_args):
        raise accounts.PairAccountError(409, "该账户地址已添加")

    monkeypatch.setattr(accounts, "add_account", duplicate)
    response = client.post("/api/v1/hyperliquid/pairs/nbis-crwv/accounts", json={"label": "重复", "address": ADDRESS})
    assert response.status_code == 409
    assert response.json()["detail"] == "该账户地址已添加"
    assert client.post("/api/v1/hyperliquid/pairs/nbis-crwv/accounts", json={"label": "bad", "address": "0x12"}).status_code == 422


def test_dashboard_uses_database_account_and_address_cache_key(client, monkeypatch) -> None:
    selected = {"id": "nbis-crwv", "account_address": SECOND}
    monkeypatch.setattr(accounts, "apply_active_accounts", lambda _url, descriptors, owner=None: [
        {**item, "account_address": SECOND} for item in descriptors
    ])
    calls = []
    monkeypatch.setattr(pairs, "_run_dashboard", lambda argv: calls.append(argv) or {
        "schema_version": 2,
        "config_revision": pairs.load_pairs()[0],
        "queried_at": "2026-10-19T08:00:00Z",
        "pair": {"id": "nbis-crwv", "label": "NBIS / CRWV", "network": "mainnet", "dex": "xyz",
                 "coins": ["xyz:NBIS", "xyz:CRWV"], "account_address": SECOND},
        "blocks": {},
    })
    monkeypatch.setattr(pairs, "_validate_v2_snapshot", lambda snapshot, descriptor, revision: snapshot)
    first = client.get("/api/v1/hyperliquid/pairs/nbis-crwv/dashboard")
    second = client.get("/api/v1/hyperliquid/pairs/nbis-crwv/dashboard")
    assert first.status_code == second.status_code == 200
    assert calls == [["--pair", "nbis-crwv", "--address", SECOND]]
    assert selected["account_address"] == SECOND
