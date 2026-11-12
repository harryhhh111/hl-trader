from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from hl_app.db import migrate
from hl_app.services import hyperliquid_pairs as pairs
from hl_app.services import hyperliquid_rebalance as rebalance
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


def market_snapshot(revision: str, first: str = "2300", second: str = "1980", state: str = "opposite") -> dict:
    items = [] if state == "flat" else [
        {"coin": "xyz:NBIS", "side": "long", "notional": first},
        {"coin": "xyz:CRWV", "side": "short", "notional": second},
    ]
    return {
        "schema_version": 2,
        "config_revision": revision,
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "pair": {
            "id": "nbis-crwv", "network": "mainnet", "dex": "xyz",
            "coins": ["xyz:NBIS", "xyz:CRWV"], "account_address": ADDRESS_LOWER,
        },
        "blocks": {"positions": {
            "status": "ok", "position_state": state, "items": items, "open_orders": 0,
        }},
    }


def preview_report(revision: str, legs: list[int]) -> dict:
    return {
        "schema_version": 1, "status": "preview", "pair_id": "nbis-crwv",
        "network": "mainnet", "config_revision": revision, "account_address": ADDRESS_LOWER,
        "target_notional": "2000", "leg_indices": legs, "created_at": datetime.now(timezone.utc).isoformat(),
        "legs": [{"coin": "xyz:NBIS"}, {"coin": "xyz:CRWV"}], "diagnostics": [],
    }


def test_candidate_requires_two_checks_and_reuses_selected_scope(configured_db) -> None:
    namespace, revision = configured_db
    rebalance.save_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "2000", "10")
    previews = []

    def preview(_url, _pair, _account, _revision, _target, _owner, legs):
        previews.append(legs)
        return preview_report(revision, legs)

    first = rebalance.monitor_once(namespace.runtime_url, lambda *_args: market_snapshot(revision), preview)
    assert first["results"][0]["status"] == "monitoring" and previews == []
    second = rebalance.monitor_once(namespace.runtime_url, lambda *_args: market_snapshot(revision), preview)
    current = rebalance.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert second["results"][0]["status"] == "candidate" and previews == [[0]], current["last_error"]
    assert current["candidate_leg_indices"] == [0]
    assert current["candidate_preview"]["leg_indices"] == [0]
    assert current["consecutive_count"] == 2
    assert [event["event_type"] for event in current["events"]].count("candidate") == 1
    rebalance.monitor_once(namespace.runtime_url, lambda *_args: market_snapshot(revision), preview)
    repeated = rebalance.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert previews == [[0]]
    assert [event["event_type"] for event in repeated["events"]].count("candidate") == 1

    report = {
        **preview_report(revision, [0]), "status": "succeeded",
        "action_id": "adjust-0x" + "a" * 32, "request_id": None,
    }
    rebalance.record_execution_result(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, None, report)
    rebalance.record_execution_result(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, None, report)
    completed = rebalance.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert completed["status"] == "cooling" and completed["cooldown_until"] is not None
    assert completed["last_action_id"] == report["action_id"]
    assert [event["event_type"] for event in completed["events"]].count("execution_updated") == 1


def test_risk_exit_cancels_and_blocks_rebalance_candidate(configured_db) -> None:
    namespace, revision = configured_db
    rebalance.save_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "2000", "10")
    preview = lambda *_args: preview_report(revision, [0])
    rebalance.monitor_once(namespace.runtime_url, lambda *_args: market_snapshot(revision), preview)
    rebalance.monitor_once(namespace.runtime_url, lambda *_args: market_snapshot(revision), preview)
    risk.save_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "300", None)
    with psycopg2.connect(namespace.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE hyperliquid_pair_risk_rules SET status = 'triggered', trigger_kind = 'take_profit'")
    result = rebalance.monitor_once(namespace.runtime_url, lambda *_args: market_snapshot(revision), preview)
    assert result["results"][0]["status"] == "monitoring"
    current = rebalance.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert current["risk_blocked"] is True and current["candidate_leg_indices"] == []
    assert current["events"][0]["event_type"] == "candidate_cleared"


def test_rule_change_invalidates_inflight_monitor_result(configured_db) -> None:
    namespace, revision = configured_db
    rebalance.save_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "2000", "10")
    with psycopg2.connect(namespace.runtime_url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM hyperliquid_rebalance_rules")
            old_rule = dict(cursor.fetchone())
    changed = rebalance.save_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "2100", "12")
    assert changed["rule_revision"] == 2
    evaluation = rebalance.evaluate_snapshot(market_snapshot(revision), old_rule)
    assert rebalance._apply_evaluation(namespace.runtime_url, old_rule, evaluation) == "rule_changed"
    current = rebalance.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert current["status"] == "monitoring" and current["candidate_leg_indices"] == []


def test_failed_monitor_resets_consecutive_confirmation(configured_db) -> None:
    namespace, revision = configured_db
    rebalance.save_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "2000", "10")
    rebalance.monitor_once(namespace.runtime_url, lambda *_args: market_snapshot(revision))
    broken = market_snapshot(revision)
    broken["blocks"]["positions"]["items"][0]["notional"] = None
    result = rebalance.monitor_once(namespace.runtime_url, lambda *_args: broken)
    assert result["results"][0]["status"] == "unavailable"
    current = rebalance.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER)
    assert current["consecutive_count"] == 0 and current["candidate_leg_indices"] == []
    assert "名义" in current["last_error"]


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
    rebalance.save_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, revision, True, "2000", "10", first_owner)
    rebalance.save_rule(namespace.runtime_url, "nbis-crwv", OTHER_ADDRESS, revision, True, "3000", "15", second_owner)
    first = rebalance.get_rule(namespace.runtime_url, "nbis-crwv", ADDRESS_LOWER, first_owner)
    second = rebalance.get_rule(namespace.runtime_url, "nbis-crwv", OTHER_ADDRESS, second_owner)
    assert first["target_notional"] == "2000" and second["threshold_pct"] == "15"
    with pytest.raises(rebalance.RebalanceError, match="当前账户已变化"):
        rebalance.get_rule(namespace.runtime_url, "nbis-crwv", OTHER_ADDRESS, first_owner)
    with psycopg2.connect(namespace.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM hyperliquid_rebalance_rules")
            assert cursor.fetchone()[0] == 2
