from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from hl_app.db import migrate
from hl_app.services import hyperliquid_pairs as pairs
from hl_app.services import hyperliquid_risk as risk

from .conftest import PostgresNamespace
from .test_hyperliquid_pairs import ADDRESS_LOWER, VALID

pytestmark = pytest.mark.db
OTHER_ADDRESS = "0x" + "1" * 40


@pytest.fixture
def configured_db(postgres_namespace: PostgresNamespace, monkeypatch, tmp_path):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    with psycopg2.connect(postgres_namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE operation_control SET mode = 'active'")
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(path))
    pairs.clear_cache()
    yield postgres_namespace, pairs.load_pairs()[0]
    pairs.clear_cache()


def market_snapshot(revision: str, pnl: str, state: str = "opposite") -> dict:
    items = [] if state == "flat" else [
        {"coin": "xyz:NBIS", "unrealized_pnl": pnl},
        {"coin": "xyz:CRWV", "unrealized_pnl": "0"},
    ]
    return {
        "schema_version": 2,
        "config_revision": revision,
        "queried_at": datetime.now(timezone.utc).isoformat(),
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
                "position_state": state,
                "unrealized_pnl": pnl,
                "items": items,
            },
        },
    }


def test_monitor_persists_trigger_once_and_records_manual_close(configured_db) -> None:
    namespace, revision = configured_db
    saved = risk.save_rule(
        namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "300", "500",
    )
    assert saved["status"] == "monitoring" and not saved["worker_available"]
    result = risk.monitor_once(namespace.runtime_url, lambda *_args: market_snapshot(revision, "300"))
    assert result["results"][0]["status"] == "triggered"
    triggered = risk.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert triggered["worker_available"] is True
    assert triggered["trigger_kind"] == "take_profit"
    assert triggered["pair_unrealized_pnl"] == "300"
    assert [event["event_type"] for event in triggered["events"]].count("triggered") == 1
    risk.monitor_once(namespace.runtime_url, lambda *_args: market_snapshot(revision, "350"))
    repeated = risk.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert [event["event_type"] for event in repeated["events"]].count("triggered") == 1
    report = {
        "pair_id": "nbis-crwv",
        "account_address": ADDRESS_LOWER,
        "target_notional": "0.0",
        "status": "succeeded",
        "action_id": "adjust-0x" + "a" * 32,
        "request_id": None,
    }
    risk.record_close_result(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, None, report)
    risk.record_close_result(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, None, report)
    completed = risk.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert completed["status"] == "completed"
    assert completed["last_action_id"] == report["action_id"]
    assert completed["events"][0]["event_type"] == "execution_updated"
    assert [event["event_type"] for event in completed["events"]].count("execution_updated") == 1


def test_rule_change_invalidates_inflight_monitor_result(configured_db) -> None:
    namespace, revision = configured_db
    risk.save_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "300", None)
    with psycopg2.connect(namespace.runtime_url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM hyperliquid_pair_risk_rules")
            old_rule = dict(cursor.fetchone())
    changed = risk.save_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "400", None)
    assert changed["rule_revision"] == 2
    evaluation = risk.evaluate_snapshot(market_snapshot(revision, "500"), old_rule)
    assert risk._apply_evaluation(namespace.runtime_url, old_rule, evaluation) == "rule_changed"
    current = risk.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert current["status"] == "monitoring" and current["trigger_kind"] is None


def test_failed_monitor_does_not_use_stale_success(configured_db) -> None:
    namespace, revision = configured_db
    risk.save_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "300", None)
    broken = market_snapshot(revision, "300")
    broken["blocks"]["positions"]["items"][0]["unrealized_pnl"] = None
    result = risk.monitor_once(namespace.runtime_url, lambda *_args: broken)
    assert result["results"][0]["status"] == "unavailable"
    current = risk.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert current["status"] == "unavailable" and current["trigger_kind"] is None
    assert "缺失盈亏" in current["last_error"]


def test_rules_are_isolated_by_login_account(configured_db) -> None:
    namespace, revision = configured_db
    with psycopg2.connect(namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO auth_accounts (email, password_hash) VALUES ('one@example.com', 'hash') RETURNING id")
            first_owner = cursor.fetchone()[0]
            cursor.execute("INSERT INTO auth_accounts (email, password_hash) VALUES ('two@example.com', 'hash') RETURNING id")
            second_owner = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO hyperliquid_pair_accounts (pair_id, label, address, is_active, owner_account_id) VALUES ('nbis-crwv', 'one', %s, true, %s)",
                (ADDRESS_LOWER, first_owner),
            )
            cursor.execute(
                "INSERT INTO hyperliquid_pair_accounts (pair_id, label, address, is_active, owner_account_id) VALUES ('nbis-crwv', 'two', %s, true, %s)",
                (OTHER_ADDRESS, second_owner),
            )
    risk.save_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "300", None, first_owner)
    risk.save_rule(namespace.runtime_url, "nbis-crwv", OTHER_ADDRESS, revision, True, None, "500", second_owner)
    first = risk.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, first_owner)
    second = risk.get_rule(namespace.runtime_url, "nbis-crwv", OTHER_ADDRESS, second_owner)
    assert first["take_profit_usd"] == "300" and second["stop_loss_usd"] == "500"
    with pytest.raises(risk.RiskError, match="当前账户已变化"):
        risk.get_rule(namespace.runtime_url, "nbis-crwv", OTHER_ADDRESS, first_owner)
    with psycopg2.connect(namespace.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM hyperliquid_pair_risk_rules")
            assert cursor.fetchone()[0] == 2
