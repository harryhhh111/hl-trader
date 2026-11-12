from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hl_app.auth import require_session
from hl_app.routes import hyperliquid
from hl_app.services import hyperliquid_history as history
from hl_app.services import hyperliquid_pairs as pairs

from .test_hyperliquid_pairs import ADDRESS_LOWER, VALID

ACCOUNT = ADDRESS_LOWER
DESCRIPTOR = {
    "id": "nbis-crwv",
    "network": "mainnet",
    "dex": "xyz",
    "coins": ["xyz:NBIS", "xyz:CRWV"],
    "account_address": ACCOUNT,
}


def leg(coin: str, action: str) -> dict:
    return {
        "coin": coin,
        "position_side": "long" if coin == "xyz:NBIS" else "short",
        "action": action,
        "reduce_only": False,
        "order_qty": "1.5",
        "protect_px": "100",
        "status": "filled",
        "confirmed_qty": "1.5",
        "avg_px": "99.5",
        "oid": 12345,
        "cloid": "0x" + "a" * 32,
        "result": "全部成交",
    }


def report(action_id: str, created_at: str, **overrides) -> dict:
    value = {
        "schema_version": 1,
        "status": "succeeded",
        "action_id": action_id,
        "request_id": "4d37d2a5-1da5-47a1-a2ce-b17365ea566f",
        "pair_id": "nbis-crwv",
        "network": "mainnet",
        "config_revision": "a" * 64,
        "account_address": ACCOUNT,
        "target_notional": "2000",
        "created_at": created_at,
        "legs": [leg("xyz:NBIS", "buy"), leg("xyz:CRWV", "sell")],
        "diagnostics": ["两腿订单均已全部成交，并已核对交易所实际持仓"],
        "signing_requests": [{"typed_data": {"must_not": "leak"}}],
    }
    value.update(overrides)
    return value


def write_report(directory, action_id: str, value: dict, current: bool = False) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    name = "adjust-state.json" if current else f"adjust-{action_id}-{value['status']}.json"
    (directory / name).write_text(json.dumps(value), encoding="utf-8")


def test_history_combines_current_and_archives_without_duplicates(tmp_path) -> None:
    directory = tmp_path / "nbis-crwv" / ACCOUNT
    current_id = "adjust-0x" + "a" * 32
    old_id = "adjust-0x" + "b" * 32
    current = report(current_id, "2026-11-04T02:00:00Z")
    write_report(directory, current_id, current, current=True)
    write_report(directory, current_id, current)
    old = report(old_id, "2026-11-03T02:00:00Z", request_id=None, expected_signer="0x" + "1" * 40)
    write_report(directory, old_id, old)
    malformed_id = "adjust-0x" + "c" * 32
    write_report(directory, malformed_id, report(malformed_id, "2026-11-02T02:00:00Z", account_address="0x" + "2" * 40))
    (directory / "unrelated.json").write_text("not a report", encoding="utf-8")

    result = history.read_history(str(tmp_path), DESCRIPTOR, ACCOUNT)

    assert [item["action_id"] for item in result["items"]] == [current_id, old_id]
    assert result["items"][0]["execution_mode"] == "managed"
    assert result["items"][1]["execution_mode"] == "browser"
    assert result["skipped_reports"] == 1
    assert result["has_more"] is False
    assert "signing_requests" not in result["items"][0]
    assert result["items"][0]["legs"][0]["oid"] == 12345


def test_history_maps_running_and_applies_limit(tmp_path) -> None:
    directory = tmp_path / "nbis-crwv" / ACCOUNT
    running_id = "adjust-0x" + "d" * 32
    old_id = "adjust-0x" + "e" * 32
    write_report(directory, running_id, report(running_id, "2026-11-04T03:00:00Z", status="executing"), current=True)
    write_report(directory, old_id, report(old_id, "2026-11-03T03:00:00Z"))

    result = history.read_history(str(tmp_path), DESCRIPTOR, ACCOUNT, limit=1)

    assert result["items"][0]["status"] == "running"
    assert result["has_more"] is True


def test_history_accepts_not_sent_legs(tmp_path) -> None:
    directory = tmp_path / "nbis-crwv" / ACCOUNT
    action_id = "adjust-0x" + "f" * 32
    value = report(action_id, "2026-11-12T07:02:25Z", status="incomplete")
    value["legs"][0].update(status="rejected", confirmed_qty="0", avg_px=None, oid=None, result="IOC 拒绝")
    value["legs"][1].update(status="not_sent", confirmed_qty="0", avg_px=None, oid=None, result="本次执行已停止，订单未发送")
    write_report(directory, action_id, value, current=True)

    result = history.read_history(str(tmp_path), DESCRIPTOR, ACCOUNT)

    assert result["skipped_reports"] == 0
    assert [leg["status"] for leg in result["items"][0]["legs"]] == ["rejected", "not_sent"]


def test_missing_history_directory_is_empty(tmp_path) -> None:
    result = history.read_history(str(tmp_path), DESCRIPTOR, ACCOUNT)
    assert result == {
        "pair_id": "nbis-crwv", "account_address": ACCOUNT, "items": [],
        "has_more": False, "skipped_reports": 0,
    }


def test_http_trade_history_contract(monkeypatch, tmp_path) -> None:
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(path))
    pairs.clear_cache()
    expected = {"pair_id": "nbis-crwv", "account_address": ACCOUNT, "items": [], "has_more": False, "skipped_reports": 0}
    monkeypatch.setattr(history, "list_history", lambda *_args: expected)
    app = FastAPI()
    app.state.settings = SimpleNamespace(database_url="postgresql://runtime")
    app.include_router(hyperliquid.router, prefix="/api/v1")
    app.dependency_overrides[require_session] = lambda: None
    client = TestClient(app)

    response = client.get(f"/api/v1/hyperliquid/pairs/nbis-crwv/trade-history?account_address={ACCOUNT}")

    assert response.status_code == 200
    assert response.json() == {"data": expected}
    pairs.clear_cache()
