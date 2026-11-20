from __future__ import annotations

import psycopg2
import pytest
from psycopg2 import errors

from hl_app.db import WebInstanceAlreadyRunning, WebLifecycle, _migration_files, connect, migrate

from .conftest import PostgresNamespace

pytestmark = pytest.mark.db


def test_connection_uses_utf8(postgres_namespace: PostgresNamespace) -> None:
    with connect(postgres_namespace.migration_url) as conn:
        assert conn.encoding == "UTF8"


def test_migration_is_idempotent_and_defaults_to_maintenance(
    postgres_namespace: PostgresNamespace,
) -> None:
    assert migrate(postgres_namespace.migration_url) == ["001_operation_control", "002_auth", "003_watchlist", "004_announcements", "005_sec_request_gate", "006_migration_controls", "007_hyperliquid_pair_accounts", "008_hyperliquid_api_wallets", "009_account_auth", "010_pair_risk_controls", "011_shared_wallet_account_scope", "012_threshold_rebalancing", "013_stock_data_news_sync", "014_watchlist_ownership_quotes"]
    assert migrate(postgres_namespace.migration_url) == []
    with psycopg2.connect(postgres_namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT mode FROM operation_control")
            assert cursor.fetchone() == ("maintenance",)


def test_watchlist_upgrade_clones_existing_items_for_every_account(
    postgres_namespace: PostgresNamespace,
) -> None:
    migrations = dict(_migration_files())
    with psycopg2.connect(postgres_namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            for version, statement in migrations.items():
                if version == "014_watchlist_ownership_quotes":
                    break
                cursor.execute(statement)
            cursor.execute(
                "INSERT INTO auth_accounts (email, password_hash) VALUES "
                "('admin@example.com', 'fixture'), ('member@example.com', 'fixture')"
            )
            for code, note in (("NBIS", "first"), ("CRWV", "second")):
                cursor.execute(
                    "INSERT INTO securities (market, stock_code, stock_name, currency, quote_source, quote_symbol, exchange_suffix) "
                    "VALUES ('US', %s, %s, 'USD', 'tencent', %s, '.OQ') RETURNING id",
                    (code, code, f"{code}.OQ"),
                )
                cursor.execute(
                    "INSERT INTO watchlist_items (security_id, note) VALUES (%s, %s)",
                    (cursor.fetchone()[0], note),
                )
            cursor.execute(migrations["014_watchlist_ownership_quotes"])
            cursor.execute(
                "SELECT s.stock_code, w.owner_account_id, w.note FROM watchlist_items w "
                "JOIN securities s ON s.id = w.security_id ORDER BY s.stock_code, w.owner_account_id"
            )
            assert cursor.fetchall() == [
                ("CRWV", 1, "second"), ("CRWV", 2, "second"),
                ("NBIS", 1, "first"), ("NBIS", 2, "first"),
            ]


def test_runtime_role_has_no_ddl_or_control_write_permission(
    postgres_namespace: PostgresNamespace,
) -> None:
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    with psycopg2.connect(postgres_namespace.runtime_url) as conn:
        with conn.cursor() as cursor:
            with pytest.raises(errors.InsufficientPrivilege):
                cursor.execute("CREATE TABLE forbidden_runtime_ddl (id INTEGER)")
        conn.rollback()
        with conn.cursor() as cursor:
            with pytest.raises(errors.InsufficientPrivilege):
                cursor.execute("UPDATE operation_control SET mode = 'active'")


def test_web_lifecycle_lock_rejects_second_process_connection(
    postgres_namespace: PostgresNamespace,
) -> None:
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    first = WebLifecycle(postgres_namespace.runtime_url)
    second = WebLifecycle(postgres_namespace.runtime_url)
    first.acquire()
    try:
        with pytest.raises(WebInstanceAlreadyRunning):
            second.acquire()
        first.ensure_healthy()
    finally:
        first.close()
        second.close()
    third = WebLifecycle(postgres_namespace.runtime_url)
    third.acquire()
    third.close()
