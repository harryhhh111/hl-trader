from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hl_app.auth import AuthError, require_session
from hl_app.routes import hyperliquid
from hl_app.services import hyperliquid_preflight as preflight
from hl_app.services.hyperliquid_adjustments import AdjustmentError

from .test_hyperliquid_leverage import configured  # noqa: F401
from .test_hyperliquid_pairs import ADDRESS_LOWER


def payload(revision):
    return {
        "schema_version": 1, "pair_id": "nbis-crwv", "network": "mainnet",
        "config_revision": revision, "account_address": ADDRESS_LOWER,
        "queried_at": datetime.now(timezone.utc).isoformat(), "policy_max_leverage": 3,
        "ready_for_increase": True,
        "legs": [
            {"coin": coin, "margin_mode": "cross", "leverage": 3, "market_max_leverage": 5,
             "cross_available": True, "ready_for_increase": True, "diagnostics": []}
            for coin in ("xyz:NBIS", "xyz:CRWV")
        ],
    }


def mock_process(monkeypatch, response, code=0):
    calls = []
    monkeypatch.setattr(preflight.subprocess, "Popen", lambda argv, **_kw: calls.append(argv) or SimpleNamespace(returncode=code))
    monkeypatch.setattr(preflight, "communicate_bounded", lambda *_args: (json.dumps(response), ""))
    return calls


def collect(revision):
    return preflight.get_preflight("postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, revision)


def test_preflight_is_read_only_fresh_and_not_cached(configured, monkeypatch):
    calls = mock_process(monkeypatch, payload(configured))
    assert collect(configured)["ready_for_increase"]
    assert collect(configured)["policy_max_leverage"] == 3
    assert len(calls) == 2
    assert calls[0][3] == "adjust-preflight"
    assert "--execute" not in calls[0] and "--secret-file" not in calls[0]


@pytest.mark.parametrize("change", [
    lambda p: p.update(account_address="0x" + "1" * 40),
    lambda p: p.update(config_revision="0" * 64),
    lambda p: p.update(pair_id="hype-btc"),
    lambda p: p.update(policy_max_leverage=4),
    lambda p: p.update(queried_at=(datetime.now(timezone.utc) - timedelta(seconds=31)).isoformat()),
    lambda p: p["legs"][0].update(leverage=4),
    lambda p: p["legs"][0].update(leverage=True),
    lambda p: p["legs"][0].update(margin_mode="isolated"),
    lambda p: p["legs"][0].update(margin_mode=[]),
    lambda p: p["legs"][0].update(ready_for_increase=False),
])
def test_invalid_or_inconsistent_results_are_rejected(configured, monkeypatch, change):
    response = payload(configured)
    change(response)
    mock_process(monkeypatch, response)
    with pytest.raises(AdjustmentError) as error:
        collect(configured)
    assert error.value.status_code == 503


def test_only_isolated_exception_and_reported_violations(configured, monkeypatch):
    response = payload(configured)
    response["legs"][0].update(margin_mode="isolated", cross_available=False)
    mock_process(monkeypatch, response)
    assert collect(configured)["ready_for_increase"]
    response["legs"][1].update(leverage=4, ready_for_increase=False, diagnostics=["请降至 3x"])
    response["ready_for_increase"] = False
    assert not collect(configured)["ready_for_increase"]


def test_collection_failure_cannot_reuse_success(configured, monkeypatch):
    mock_process(monkeypatch, payload(configured))
    assert collect(configured)["ready_for_increase"]
    mock_process(monkeypatch, {}, code=1)
    with pytest.raises(AdjustmentError, match="禁止增仓"):
        collect(configured)


def test_identity_is_checked_again_after_collection(configured, monkeypatch):
    calls = mock_process(monkeypatch, payload(configured))
    original = preflight.adjustments._resolve
    resolutions = []

    def resolve(*args):
        resolutions.append(args)
        if len(resolutions) == 2:
            raise AdjustmentError(409, "账户已切换")
        return original(*args)

    monkeypatch.setattr(preflight.adjustments, "_resolve", resolve)
    with pytest.raises(AdjustmentError, match="账户已切换"):
        collect(configured)
    assert len(calls) == 1


def test_http_preflight_is_authenticated_and_returns_data(configured, monkeypatch):
    app = FastAPI()
    app.state.settings = SimpleNamespace(database_url="postgresql://runtime", auth_mode="token")
    app.state.web_lifecycle = SimpleNamespace(ensure_healthy=lambda: None)

    def reject_auth(_credential):
        raise AuthError(401, "未登录")

    app.state.auth = SimpleNamespace(authenticate=reject_auth)
    app.include_router(hyperliquid.router, prefix="/api/v1")
    app.dependency_overrides[require_session] = lambda: None
    monkeypatch.setattr(preflight, "get_preflight", lambda *_args: payload(configured))
    client = TestClient(app)
    response = client.get("/api/v1/hyperliquid/pairs/nbis-crwv/adjustment-preflight", params={
        "account_address": ADDRESS_LOWER, "config_revision": configured,
    })
    assert response.status_code == 200
    assert response.json()["data"]["ready_for_increase"]
    app.dependency_overrides.clear()
    assert client.get("/api/v1/hyperliquid/pairs/nbis-crwv/adjustment-preflight", params={
        "account_address": ADDRESS_LOWER, "config_revision": configured,
    }).status_code == 401
