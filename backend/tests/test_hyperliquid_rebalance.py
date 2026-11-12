from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hl_app import workers
from hl_app.auth import require_session
from hl_app.routes import hyperliquid
from hl_app.services import hyperliquid_accounts as accounts
from hl_app.services import hyperliquid_pairs as pairs
from hl_app.services import hyperliquid_rebalance as rebalance

from .test_hyperliquid_pairs import ADDRESS_LOWER, VALID

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


@pytest.fixture
def configured(monkeypatch, tmp_path):
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(path))
    monkeypatch.setattr(accounts, "apply_active_accounts", lambda _url, descriptors, owner=None: descriptors)
    pairs.clear_cache()
    yield pairs.load_pairs()[0]
    pairs.clear_cache()


def rule(**changes) -> dict:
    value = {
        "id": 1,
        "owner_account_id": None,
        "pair_id": "nbis-crwv",
        "network": "mainnet",
        "account_address": ADDRESS_LOWER,
        "target_notional": Decimal("2000"),
        "threshold_pct": Decimal("10"),
    }
    value.update(changes)
    return value


def snapshot(first: str | None, second: str | None, **changes) -> dict:
    items = []
    if first is not None:
        items.append({"coin": "xyz:NBIS", "side": "long", "notional": first})
    if second is not None:
        items.append({"coin": "xyz:CRWV", "side": "short", "notional": second})
    state = "flat" if not items else "opposite" if len(items) == 2 else "single_leg"
    value = {
        "schema_version": 2,
        "config_revision": pairs.load_pairs()[0],
        "queried_at": NOW.isoformat(),
        "pair": {
            "id": "nbis-crwv", "network": "mainnet", "dex": "xyz",
            "coins": ["xyz:NBIS", "xyz:CRWV"], "account_address": ADDRESS_LOWER,
        },
        "blocks": {"positions": {
            "status": "ok", "position_state": state, "items": items, "open_orders": 0,
        }},
    }
    value.update(changes)
    return value


def test_drift_uses_absolute_target_distance_and_threshold_boundary(configured) -> None:
    result = rebalance.evaluate_snapshot(snapshot("2300", "1980"), rule(), NOW)
    assert result["drift_pct"] == ["15", "1"]
    assert result["observed_leg_indices"] == [0]
    assert rebalance.evaluate_snapshot(snapshot("1800", "2200"), rule(), NOW)["observed_leg_indices"] == [0, 1]
    assert rebalance.evaluate_snapshot(snapshot("2201", "1799"), rule(), NOW)["observed_leg_indices"] == [0, 1]


def test_flat_is_not_a_new_opening_signal(configured) -> None:
    result = rebalance.evaluate_snapshot(snapshot(None, None), rule(), NOW)
    assert result["current_notionals"] == ["0", "0"]
    assert result["drift_pct"] == ["100", "100"]
    assert result["observed_leg_indices"] == []


def test_invalid_direction_missing_notional_and_stale_data_never_candidate(configured) -> None:
    same_direction = snapshot("2300", "1980")
    same_direction["blocks"]["positions"]["position_state"] = "same_direction"
    with pytest.raises(rebalance.RebalanceError, match="同向"):
        rebalance.evaluate_snapshot(same_direction, rule(), NOW)
    missing = snapshot("2300", "1980")
    missing["blocks"]["positions"]["items"][0]["notional"] = None
    with pytest.raises(rebalance.RebalanceError, match="名义"):
        rebalance.evaluate_snapshot(missing, rule(), NOW)
    stale = snapshot("2300", "1980", queried_at=(NOW - timedelta(seconds=31)).isoformat())
    with pytest.raises(rebalance.RebalanceError, match="过期"):
        rebalance.evaluate_snapshot(stale, rule(), NOW)


def test_http_rebalance_rule_contract(configured, monkeypatch) -> None:
    app = FastAPI()
    app.state.settings = SimpleNamespace(database_url="postgresql://runtime")
    app.include_router(hyperliquid.router, prefix="/api/v1")
    app.dependency_overrides[require_session] = lambda: None
    client = TestClient(app)
    monkeypatch.setattr(rebalance, "get_rule", lambda *_args: {"configured": False, "status": "disabled"})
    saved = []

    def save(*args):
        saved.append(args)
        return {"configured": True, "status": "monitoring"}

    monkeypatch.setattr(rebalance, "save_rule", save)
    response = client.get(f"/api/v1/hyperliquid/pairs/nbis-crwv/rebalance?account_address={ADDRESS_LOWER}")
    assert response.status_code == 200
    body = {
        "account_address": ADDRESS_LOWER, "config_revision": configured,
        "enabled": True, "target_notional": "2000", "threshold_pct": "10",
    }
    response = client.put("/api/v1/hyperliquid/pairs/nbis-crwv/rebalance", json=body)
    assert response.status_code == 200
    assert saved[0][4:7] == (True, "2000", "10")
    assert client.put("/api/v1/hyperliquid/pairs/nbis-crwv/rebalance", json={**body, "unexpected": 1}).status_code == 422


def test_rebalance_worker_is_independent_of_stock_collectors(monkeypatch) -> None:
    monkeypatch.setenv("HL_APP_COLLECTORS_ENABLED", "false")
    monkeypatch.setenv("HL_APP_REBALANCE_MONITOR_ENABLED", "true")
    calls = []

    class Lifecycle:
        def ensure_healthy(self):
            calls.append("healthy")

    monkeypatch.setattr(workers, "assert_active", lambda _url: calls.append("active"))
    monkeypatch.setattr(workers.hyperliquid_rebalance, "monitor_once", lambda _url: {"kind": "pair_rebalance", "checked": 0})
    result = workers._run_once("postgresql://runtime", "pair_rebalance", Lifecycle())
    assert result["kind"] == "pair_rebalance"
    assert calls == ["healthy", "active"]
    monkeypatch.setenv("HL_APP_REBALANCE_MONITOR_ENABLED", "false")
    assert workers._run_once("postgresql://runtime", "pair_rebalance", Lifecycle())["status"] == "disabled"
