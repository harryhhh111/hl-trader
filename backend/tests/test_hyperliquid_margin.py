from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hl_app.auth import require_session
from hl_app.routes import hyperliquid
from hl_app.services import hyperliquid_accounts as accounts
from hl_app.services import hyperliquid_margin as margin
from hl_app.services import hyperliquid_pairs as pairs

from .test_hyperliquid_pairs import ADDRESS_LOWER, VALID

API_WALLET = "0x1111111111111111111111111111111111111111"
ACTION_ID = "margin-0x" + "a" * 32
SIGNATURE = "0x" + "1" * 130


@pytest.fixture
def configured(monkeypatch, tmp_path):
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(path))
    monkeypatch.setenv("HL_APP_TRADER_BIN", "/bin/hl")
    monkeypatch.setenv("HL_APP_EXECUTION_STATE_DIR", "/var/lib/hl-trader")
    monkeypatch.setattr(accounts, "apply_active_accounts", lambda _url, descriptors, owner=None: descriptors)
    monkeypatch.setattr(margin, "_require_no_active_execution", lambda *_args: None)
    pairs.clear_cache()
    yield pairs.load_pairs()[0]
    pairs.clear_cache()


def report(revision: str, status: str = "prepared") -> dict:
    return {
        "schema_version": 1,
        "status": status,
        "action_id": ACTION_ID,
        "pair_id": "nbis-crwv",
        "network": "mainnet",
        "config_revision": revision,
        "account_address": ADDRESS_LOWER,
        "coin": "xyz:NBIS",
        "position_size": "10",
        "position_notional": "100",
        "current_margin": "50",
        "margin_delta": "30",
        "target_margin": "80",
        "target_ratio_pct": "80",
        "expected_signer": API_WALLET,
        "nonce": 1,
        "typed_data": {"types": {}},
        "result": None,
    }


def test_browser_prepare_uses_fixed_command_and_no_secret(configured, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(margin, "_invoke", lambda argv, *_args: calls.append(argv) or report(configured))
    result = margin.prepare_browser(
        "postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured, "xyz:NBIS", API_WALLET,
    )
    assert result["status"] == "prepared"
    assert calls[0][3] == "isolated-margin-prepare"
    assert "--secret-file" not in calls[0] and "--secret-fd" not in calls[0]


def test_browser_submit_checks_signature_envelope_and_identity(configured, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(margin, "_invoke", lambda argv, *_args: calls.append(argv) or report(configured, "succeeded"))
    result = margin.submit_browser(
        "postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured,
        "xyz:NBIS", API_WALLET, ACTION_ID, SIGNATURE,
    )
    assert result["status"] == "succeeded"
    assert calls[0][3] == "isolated-margin-submit"
    assert calls[0][calls[0].index("--signature") + 1] == SIGNATURE
    with pytest.raises(Exception, match="65 字节"):
        margin.submit_browser(
            "postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured,
            "xyz:NBIS", API_WALLET, ACTION_ID, "0x12",
        )


def test_invalid_coin_is_rejected_before_process(configured, monkeypatch) -> None:
    monkeypatch.setattr(margin, "_invoke", lambda *_args: pytest.fail("must not invoke"))
    with pytest.raises(Exception, match="不属于"):
        margin.prepare_browser(
            "postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured, "BTC", API_WALLET,
        )


def test_http_isolated_margin_contract(configured, monkeypatch) -> None:
    app = FastAPI()
    app.state.settings = SimpleNamespace(database_url="postgresql://runtime")
    app.include_router(hyperliquid.router, prefix="/api/v1")
    app.dependency_overrides[require_session] = lambda: None
    client = TestClient(app)
    monkeypatch.setattr(margin, "prepare_browser", lambda *_args: report(configured))
    monkeypatch.setattr(margin, "submit_browser", lambda *_args: report(configured, "succeeded"))
    body = {
        "account_address": ADDRESS_LOWER, "config_revision": configured,
        "coin": "xyz:NBIS", "expected_signer": API_WALLET,
    }
    assert client.post("/api/v1/hyperliquid/pairs/nbis-crwv/isolated-margin/browser-prepare", json=body).status_code == 200
    assert client.post(
        "/api/v1/hyperliquid/pairs/nbis-crwv/isolated-margin/browser-submit",
        json={**body, "action_id": ACTION_ID, "signature": SIGNATURE},
    ).status_code == 200
    assert client.post(
        "/api/v1/hyperliquid/pairs/nbis-crwv/isolated-margin/browser-prepare",
        json={**body, "unexpected": True},
    ).status_code == 422


def test_managed_confirmation_and_payload_include_operation(configured) -> None:
    descriptor = next(item for item in pairs.load_pairs()[1] if item["id"] == "nbis-crwv")
    first = margin._payload_hash(descriptor, ADDRESS_LOWER, configured, "xyz:NBIS", 1)
    changed = margin._payload_hash(descriptor, ADDRESS_LOWER, configured, "xyz:CRWV", 1)
    assert first != changed
    with pytest.raises(Exception, match="确认内容"):
        margin.execute_managed(
            "postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured,
            "xyz:NBIS", "wrong", "123e4567-e89b-42d3-a456-426614174000", 1,
            SimpleNamespace(account_id=None),
        )
