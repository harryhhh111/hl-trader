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
from hl_app.services import hyperliquid_risk as risk

from .test_hyperliquid_pairs import ADDRESS_LOWER, VALID

NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


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
        "take_profit_usd": Decimal("300"),
        "stop_loss_usd": Decimal("500"),
        "status": "monitoring",
        "trigger_kind": None,
        "triggered_at": None,
        "trigger_pnl": None,
        "revision": 1,
    }
    value.update(changes)
    return value


def snapshot(first: str, second: str, **changes) -> dict:
    value = {
        "schema_version": 2,
        "config_revision": pairs.load_pairs()[0],
        "queried_at": NOW.isoformat(),
        "pair": {
            "id": "nbis-crwv",
            "network": "mainnet",
            "dex": "xyz",
            "coins": ["xyz:NBIS", "xyz:CRWV"],
            "account_address": ADDRESS_LOWER,
        },
        "blocks": {
            "positions": {
                "status": "ok",
                "position_state": "opposite",
                "unrealized_pnl": str(Decimal(first) + Decimal(second)),
                "items": [
                    {"coin": "xyz:NBIS", "unrealized_pnl": first},
                    {"coin": "xyz:CRWV", "unrealized_pnl": second},
                ],
            },
        },
    }
    value.update(changes)
    return value


def test_pair_pnl_uses_both_legs_and_threshold_boundaries(configured) -> None:
    assert risk.evaluate_snapshot(snapshot("800", "-500"), rule(), NOW)["trigger_kind"] == "take_profit"
    assert risk.evaluate_snapshot(snapshot("200", "99.99"), rule(), NOW)["trigger_kind"] is None
    assert risk.evaluate_snapshot(snapshot("-200", "-300"), rule(), NOW)["trigger_kind"] == "stop_loss"
    assert risk.evaluate_snapshot(snapshot("-200", "-299.99"), rule(), NOW)["trigger_kind"] is None


def test_missing_or_inconsistent_pnl_never_triggers(configured) -> None:
    missing = snapshot("800", "-500")
    missing["blocks"]["positions"]["items"][1]["unrealized_pnl"] = None
    with pytest.raises(risk.RiskError, match="缺失盈亏"):
        risk.evaluate_snapshot(missing, rule(), NOW)
    inconsistent = snapshot("800", "-500")
    inconsistent["blocks"]["positions"]["unrealized_pnl"] = "9999"
    with pytest.raises(risk.RiskError, match="校验不一致"):
        risk.evaluate_snapshot(inconsistent, rule(), NOW)


def test_stale_or_wrong_account_snapshot_never_triggers(configured) -> None:
    stale = snapshot("800", "-500", queried_at=(NOW - timedelta(seconds=31)).isoformat())
    with pytest.raises(risk.RiskError, match="过期"):
        risk.evaluate_snapshot(stale, rule(), NOW)
    wrong = snapshot("800", "-500")
    wrong["pair"]["account_address"] = "0x" + "1" * 40
    with pytest.raises(risk.RiskError, match="身份"):
        risk.evaluate_snapshot(wrong, rule(), NOW)


def test_http_risk_rule_contract(configured, monkeypatch) -> None:
    app = FastAPI()
    app.state.settings = SimpleNamespace(database_url="postgresql://runtime")
    app.include_router(hyperliquid.router, prefix="/api/v1")
    app.dependency_overrides[require_session] = lambda: None
    client = TestClient(app)
    monkeypatch.setattr(risk, "get_rule", lambda *_args: {"configured": False, "status": "disabled"})
    saved = []

    def save(*args):
        saved.append(args)
        return {"configured": True, "status": "monitoring"}

    monkeypatch.setattr(risk, "save_rule", save)
    response = client.get(f"/api/v1/hyperliquid/pairs/nbis-crwv/risk?account_address={ADDRESS_LOWER}")
    assert response.status_code == 200
    body = {
        "account_address": ADDRESS_LOWER,
        "config_revision": configured,
        "enabled": True,
        "take_profit_usd": "300",
        "stop_loss_usd": "500",
    }
    response = client.put("/api/v1/hyperliquid/pairs/nbis-crwv/risk", json=body)
    assert response.status_code == 200
    assert saved[0][4:7] == (True, "300", "500")
    assert client.put("/api/v1/hyperliquid/pairs/nbis-crwv/risk", json={**body, "unexpected": 1}).status_code == 422


def test_pair_risk_worker_is_independent_of_stock_collectors(monkeypatch) -> None:
    monkeypatch.setenv("HL_APP_COLLECTORS_ENABLED", "false")
    monkeypatch.setenv("HL_APP_RISK_MONITOR_ENABLED", "true")
    calls = []

    class Lifecycle:
        def ensure_healthy(self):
            calls.append("healthy")

    monkeypatch.setattr(workers, "assert_active", lambda _url: calls.append("active"))
    monkeypatch.setattr(workers.hyperliquid_risk, "monitor_once", lambda _url: {"kind": "pair_risk", "checked": 0})
    result = workers._run_once("postgresql://runtime", "pair_risk", Lifecycle())
    assert result["kind"] == "pair_risk"
    assert calls == ["healthy", "active"]
    monkeypatch.setenv("HL_APP_RISK_MONITOR_ENABLED", "false")
    assert workers._run_once("postgresql://runtime", "pair_risk", Lifecycle())["status"] == "disabled"
