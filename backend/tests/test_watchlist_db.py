from __future__ import annotations

from datetime import date, datetime, timezone

import psycopg2
import pytest

from hl_app.collectors.quotes import collect_security, enabled_securities
from hl_app.db import migrate
from hl_app.services.watchlist import WatchlistError, list_items, quote_batch, update_item

from .conftest import PostgresNamespace

pytestmark = pytest.mark.db


@pytest.fixture
def seeded(postgres_namespace: PostgresNamespace):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    with psycopg2.connect(postgres_namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            rows = [
                ("US", "000001", "US Example", "USD", "000001.OQ", ".OQ", "0000001234"),
                ("CN_A", "000001", "A Example", "CNY", "sz000001", None, None),
                ("CN_HK", "00001", "HK Example", "HKD", "hk00001", None, None),
                ("US", "DISABLED", "Disabled", "USD", "DISABLED.N", ".N", "0000001234"),
            ]
            for index, row in enumerate(rows, start=1):
                market, code, name, currency, symbol, suffix, cik = row
                cursor.execute(
                    "INSERT INTO securities (market, stock_code, stock_name, currency, quote_source, quote_symbol, exchange_suffix, cik) VALUES (%s, %s, %s, %s, 'tencent', %s, %s, %s) RETURNING id",
                    (market, code, name, currency, symbol, suffix, cik),
                )
                security_id = cursor.fetchone()[0]
                cursor.execute(
                    "INSERT INTO watchlist_items (security_id, group_name, note, enabled) VALUES (%s, %s, %s, %s)",
                    (security_id, "research" if index == 1 else "", "initial" if index == 1 else "", index != 4),
                )
            cursor.execute(
                "INSERT INTO daily_quotes (market, stock_code, trade_date, close, source, currency) VALUES ('US', '000001', '2026-10-01', 100, 'fixture', 'USD'), ('US', '000001', '2026-10-02', 102, 'fixture', 'USD'), ('CN_A', '000001', '2026-10-02', 10, 'fixture', 'CNY')"
            )
    return postgres_namespace


def test_three_markets_and_same_code_do_not_mix(seeded) -> None:
    us = quote_batch(
        seeded.runtime_url, "US", ["000001"], datetime(2026, 10, 2, 23, tzinfo=timezone.utc)
    )["quotes"][0]
    cn = quote_batch(
        seeded.runtime_url, "CN_A", ["000001"], datetime(2026, 10, 2, 23, tzinfo=timezone.utc)
    )["quotes"][0]
    assert us["close"] == 102 and us["change_pct"] == pytest.approx(2)
    assert cn["close"] == 10 and cn["status"] == "missing_previous"
    items = list_items(seeded.runtime_url)
    assert len(items) == 3
    assert "DISABLED" not in {item["stock_code"] for item in items}


def test_patch_is_metadata_only_and_maintenance_blocks_write(seeded) -> None:
    with pytest.raises(WatchlistError, match="维护状态"):
        update_item(
            seeded.runtime_url,
            1,
            {"group_name": "new", "note": "reason", "research_url": "https://example.com", "sort_order": 2},
        )
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE operation_control SET mode = 'active'")
    result = update_item(
        seeded.runtime_url,
        1,
        {"group_name": "new", "note": "reason", "research_url": "https://example.com", "sort_order": 2},
    )
    assert result == {"id": 1}
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT group_name, note, research_url, sort_order, enabled FROM watchlist_items WHERE id = 1")
            assert cursor.fetchone() == ("new", "reason", "https://example.com", 2, True)


def test_collect_only_enabled_and_disable_recheck(seeded) -> None:
    targets = enabled_securities(seeded.runtime_url)
    assert len(targets) == 3 and all(target["stock_code"] != "DISABLED" for target in targets)
    disabled = dict(targets[0])
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE watchlist_items SET enabled = false WHERE security_id = %s", (disabled["id"],))

    def no_network(_url, _params):
        raise AssertionError("disabled security must not fetch")

    assert collect_security(seeded.runtime_url, disabled, http_get=no_network) == 0


def test_known_quote_is_not_overwritten_by_missing_value(seeded) -> None:
    security = enabled_securities(seeded.runtime_url)[0]

    def fixture_get(_url, _params):
        return {"data": {"us": {"day": [["2026-10-02", "100", None, "103", "99", "100"]]}}}

    count = collect_security(
        seeded.runtime_url,
        security,
        now=datetime(2026, 10, 3, 0, tzinfo=timezone.utc),
        http_get=fixture_get,
    )
    assert count == 1
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT close FROM daily_quotes WHERE market = 'US' AND stock_code = '000001' AND trade_date = '2026-10-02'")
            assert cursor.fetchone()[0] == 102


def test_invalid_security_identity_is_rejected_by_db(seeded) -> None:
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            with pytest.raises(Exception):
                cursor.execute(
                    "INSERT INTO securities (market, stock_code, stock_name, currency, quote_source, quote_symbol) VALUES ('CN_HK', '700', 'bad', 'HKD', 'tencent', 'hk00700')"
                )
        conn.rollback()
        with conn.cursor() as cursor:
            with pytest.raises(Exception):
                cursor.execute(
                    "INSERT INTO securities (market, stock_code, stock_name, currency, quote_source, quote_symbol, exchange_suffix, cik) VALUES ('US', 'BAD', 'bad', 'USD', 'tencent', 'BAD.N', '.N', 'bad')"
                )
