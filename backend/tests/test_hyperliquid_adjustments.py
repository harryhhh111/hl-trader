from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hl_app.auth import require_session
from hl_app.routes import hyperliquid
from hl_app.services import hyperliquid_accounts as accounts
from hl_app.services import hyperliquid_adjustments as adjustments
from hl_app.services import hyperliquid_pairs as pairs

from .test_hyperliquid_pairs import ADDRESS_LOWER, VALID


@pytest.fixture
def configured(monkeypatch, tmp_path):
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(path))
    monkeypatch.setenv("HL_APP_TRADER_BIN", "/bin/hl")
    monkeypatch.setenv("HL_APP_MAX_LEG_NOTIONAL_USD", "5000")
    monkeypatch.delenv("HL_APP_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("HL_APP_AGENT_SECRET_FILE", raising=False)
    monkeypatch.delenv("HL_APP_EXECUTION_STATE_DIR", raising=False)
    monkeypatch.setattr(accounts, "apply_active_accounts", lambda _url, descriptors: descriptors)
    pairs.clear_cache()
    yield pairs.load_pairs()[0]
    pairs.clear_cache()


def report(revision: str, status: str = "preview") -> dict:
    return {
        "schema_version": 1,
        "status": status,
        "action_id": None,
        "pair_id": "nbis-crwv",
        "network": "mainnet",
        "config_revision": revision,
        "account_address": ADDRESS_LOWER,
        "target_notional": "2000",
        "created_at": "2026-10-30T00:00:00Z",
        "legs": [{"coin": "xyz:NBIS"}, {"coin": "xyz:CRWV"}],
        "diagnostics": [],
    }


def test_preview_resolves_active_identity_and_passes_decimal_target(configured, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(adjustments, "_run", lambda descriptor, account, target, execute: calls.append(
        (descriptor["id"], account, target, execute),
    ) or report(configured))
    result = adjustments.preview("postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured, "2000")
    assert calls == [("nbis-crwv", ADDRESS_LOWER, Decimal("2000"), False)]
    assert result["execution_enabled"] is False
    assert result["browser_wallet_enabled"] is True
    assert result["max_leg_notional_usd"] == "5000"


def test_default_leg_notional_limit_is_15000(monkeypatch) -> None:
    monkeypatch.delenv("HL_APP_MAX_LEG_NOTIONAL_USD", raising=False)
    assert adjustments._maximum() == Decimal("15000")


def test_stale_revision_or_account_is_rejected_before_running(configured, monkeypatch) -> None:
    monkeypatch.setattr(adjustments, "_run", lambda *_args: pytest.fail("must not run"))
    with pytest.raises(adjustments.AdjustmentError, match="配置已变化") as error:
        adjustments.preview("postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, "0" * 64, "2000")
    assert error.value.status_code == 409
    with pytest.raises(adjustments.AdjustmentError, match="账户已变化") as error:
        adjustments.preview("postgresql://runtime", "nbis-crwv", "0x" + "1" * 40, configured, "2000")
    assert error.value.status_code == 409


def test_execute_requires_exact_confirmation_and_clears_snapshot_cache(configured, monkeypatch) -> None:
    with pytest.raises(adjustments.AdjustmentError, match="确认金额"):
        adjustments.execute("postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured, "2000", "1000")
    calls = []
    monkeypatch.setattr(adjustments, "_run", lambda descriptor, account, target, execute: calls.append(execute) or report(configured, "succeeded"))
    cleared = []
    monkeypatch.setattr(pairs, "clear_runtime_cache", lambda: cleared.append(True))
    result = adjustments.execute("postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured, "2000", "2000")
    assert result["status"] == "succeeded"
    assert calls == [True]
    assert cleared == [True]


def test_execution_is_disabled_by_default_before_process_start(configured) -> None:
    descriptor = next(item for item in pairs.load_pairs()[1] if item["id"] == "nbis-crwv")
    with pytest.raises(adjustments.AdjustmentError, match="真实调仓未开启") as error:
        adjustments._run(descriptor, ADDRESS_LOWER, Decimal("2000"), True)
    assert error.value.status_code == 503


def test_known_unknown_process_report_is_returned_not_hidden(configured, monkeypatch) -> None:
    descriptor = next(item for item in pairs.load_pairs()[1] if item["id"] == "nbis-crwv")
    monkeypatch.setenv("HL_APP_TRADING_ENABLED", "true")
    monkeypatch.setenv("HL_APP_AGENT_SECRET_FILE", "/run/agent.key")
    monkeypatch.setenv("HL_APP_EXECUTION_STATE_DIR", "/var/lib/hl-trader")
    monkeypatch.setattr(adjustments.subprocess, "Popen", lambda *_args, **_kwargs: type("Process", (), {"returncode": 3})())
    monkeypatch.setattr(adjustments, "communicate_bounded", lambda *_args: (json.dumps(report(configured, "unknown")), ""))
    result = adjustments._run(descriptor, ADDRESS_LOWER, Decimal("2000"), True)
    assert result["status"] == "unknown"


def test_result_identity_mismatch_is_rejected(configured) -> None:
    descriptor = next(item for item in pairs.load_pairs()[1] if item["id"] == "nbis-crwv")
    malformed = report(configured)
    malformed["account_address"] = "0x" + "1" * 40
    with pytest.raises(adjustments.AdjustmentError, match="身份校验失败"):
        adjustments._parse_report(json.dumps(malformed), descriptor, ADDRESS_LOWER, Decimal("2000"))


def test_http_preview_and_execute_contract(configured, monkeypatch) -> None:
    app = FastAPI()
    app.state.settings = SimpleNamespace(database_url="postgresql://runtime")
    app.include_router(hyperliquid.router, prefix="/api/v1")
    app.dependency_overrides[require_session] = lambda: None
    client = TestClient(app)
    monkeypatch.setattr(adjustments, "_run", lambda _descriptor, _account, _target, execute: report(
        configured, "succeeded" if execute else "preview",
    ))
    body = {
        "target_notional": "2000",
        "account_address": ADDRESS_LOWER,
        "config_revision": configured,
    }
    preview = client.post("/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/preview", json=body)
    assert preview.status_code == 200
    assert preview.json()["data"]["status"] == "preview"
    assert preview.json()["data"]["execution_enabled"] is False
    execute = client.post(
        "/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/execute",
        json={**body, "confirmation": "2000"},
    )
    assert execute.status_code == 200
    assert execute.json()["data"]["status"] == "succeeded"
    assert client.post(
        "/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/preview",
        json={**body, "unexpected": True},
    ).status_code == 422


API_WALLET = "0x1111111111111111111111111111111111111111"
ACTION_ID = "adjust-0x" + "a" * 32
SIGNATURE = "0x" + "1" * 130


def browser_report(revision: str, status: str = "prepared") -> dict:
    return {
        **report(revision, status),
        "action_id": ACTION_ID,
        "expected_signer": API_WALLET,
    }


def test_browser_prepare_needs_no_server_secret(configured, monkeypatch) -> None:
    monkeypatch.setenv("HL_APP_EXECUTION_STATE_DIR", "/var/lib/hl-trader")
    calls = []
    monkeypatch.setattr(adjustments, "_invoke", lambda argv, _descriptor, _account, _target, execute: calls.append(
        (argv, execute),
    ) or browser_report(configured))
    result = adjustments.prepare_browser(
        "postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured, "2000", API_WALLET,
    )
    assert result["status"] == "prepared"
    assert calls[0][0][3] == "adjust-prepare"
    assert calls[0][1] is False
    assert "--secret-file" not in calls[0][0]


def test_browser_rejects_original_account_and_bad_signature(configured, monkeypatch) -> None:
    monkeypatch.setattr(adjustments, "_invoke", lambda *_args: pytest.fail("must not invoke"))
    with pytest.raises(adjustments.AdjustmentError, match="必须不同"):
        adjustments.prepare_browser(
            "postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured, "2000", ADDRESS_LOWER,
        )
    with pytest.raises(adjustments.AdjustmentError, match="65 字节"):
        adjustments.submit_browser(
            "postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured, "2000", API_WALLET,
            ACTION_ID, [{"leg_index": 0, "signature": "0x12"}],
        )


def test_browser_submit_passes_verified_signature_envelope(configured, monkeypatch) -> None:
    monkeypatch.setenv("HL_APP_EXECUTION_STATE_DIR", "/var/lib/hl-trader")
    calls = []
    monkeypatch.setattr(adjustments, "_invoke", lambda argv, _descriptor, _account, _target, execute: calls.append(
        (argv, execute),
    ) or browser_report(configured, "succeeded"))
    result = adjustments.submit_browser(
        "postgresql://runtime", "nbis-crwv", ADDRESS_LOWER, configured, "2000", API_WALLET,
        ACTION_ID, [{"leg_index": 0, "signature": SIGNATURE}],
    )
    assert result["status"] == "succeeded"
    assert calls[0][0][3] == "adjust-submit"
    assert calls[0][1] is True
    assert json.loads(calls[0][0][calls[0][0].index("--signatures") + 1]) == [
        {"leg_index": 0, "signature": SIGNATURE},
    ]


def test_http_browser_prepare_and_submit_contract(configured, monkeypatch) -> None:
    monkeypatch.setenv("HL_APP_EXECUTION_STATE_DIR", "/var/lib/hl-trader")
    app = FastAPI()
    app.state.settings = SimpleNamespace(database_url="postgresql://runtime")
    app.include_router(hyperliquid.router, prefix="/api/v1")
    app.dependency_overrides[require_session] = lambda: None
    client = TestClient(app)

    def invoke(argv, _descriptor, _account, _target, execute):
        status = "prepared" if "adjust-prepare" in argv else "succeeded"
        return browser_report(configured, status)

    monkeypatch.setattr(adjustments, "_invoke", invoke)
    body = {
        "target_notional": "2000",
        "account_address": ADDRESS_LOWER,
        "config_revision": configured,
        "expected_signer": API_WALLET,
    }
    prepared = client.post("/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/browser-prepare", json=body)
    assert prepared.status_code == 200
    assert prepared.json()["data"]["status"] == "prepared"
    submitted = client.post(
        "/api/v1/hyperliquid/pairs/nbis-crwv/adjustments/browser-submit",
        json={**body, "action_id": ACTION_ID, "signatures": [{"leg_index": 0, "signature": SIGNATURE}]},
    )
    assert submitted.status_code == 200
    assert submitted.json()["data"]["status"] == "succeeded"
