from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from hl_app.collectors.quotes import collect_security
from hl_app.collectors.sources import collect_source
from hl_app.db import connect, migrate
from hl_app.migration.common import BundleError
from hl_app.migration.service import MaintenanceMode, business_write_guard, export_baseline, maintenance

pytestmark = pytest.mark.db
NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
FEED = b"<rss><channel><item><title>Raw</title><link>https://example.com/raw</link><guid>raw-1</guid></item></channel></rss>"
SOURCE = {"source_id": 1, "source_kind": "media", "issuer_identifier": "", "entry_url": "https://example.com/feed", "parser_version": 1}


@pytest.fixture
def active(postgres_namespace):
    ns = postgres_namespace
    migrate(ns.migration_url)
    ns.grant_runtime()
    with connect(ns.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE operation_control SET mode = 'active'")
            cursor.execute("INSERT INTO securities (market, stock_code, stock_name, currency, quote_source, quote_symbol, exchange_suffix) VALUES ('US', 'ORCL', 'Oracle', 'USD', 'tencent', 'ORCL.N', '.N') RETURNING id")
            security_id = cursor.fetchone()[0]
            cursor.execute("INSERT INTO watchlist_items (security_id) VALUES (%s)", (security_id,))
            cursor.execute("INSERT INTO watchlist_announcement_source (source_id, market, source_kind, issuer_identifier, allowed_domain, entry_url, enabled, source_status) VALUES (1, 'US', 'media', '', 'example.com', 'https://example.com/feed', true, 'enabled')")
    return ns, {"id": security_id, "market": "US", "stock_code": "ORCL", "quote_symbol": "ORCL.N", "currency": "USD"}


def test_maintenance_timeout_blocks_export_and_inflight_quote_write(active, tmp_path) -> None:
    ns, security = active
    fetching, resume = threading.Event(), threading.Event()

    def get(_url, _params):
        fetching.set()
        assert resume.wait(5)
        return {"data": {"us": {"day": [["2026-10-20", "100", "101", "102", "99", "100"]]}}}

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect_security, ns.runtime_url, security, NOW, get)
        try:
            assert fetching.wait(5)
            assert maintenance(ns.migration_url, timeout_seconds=0.05)["drained"] is False
            with pytest.raises(BundleError, match="未排空"):
                export_baseline(ns.migration_url, tmp_path / "baseline")
        finally:
            resume.set()
        with pytest.raises(MaintenanceMode):
            future.result(timeout=5)
    assert maintenance(ns.migration_url)["drained"] is True
    with connect(ns.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM daily_quotes")
            assert cursor.fetchone()[0] == 0


@pytest.mark.parametrize("failed", [False, True])
def test_maintenance_blocks_source_records_and_failure_bookkeeping(active, failed) -> None:
    ns, _ = active
    fetching, resume = threading.Event(), threading.Event()

    def get(_url):
        fetching.set()
        assert resume.wait(5)
        if failed:
            raise RuntimeError("fetch failed during maintenance")
        return FEED

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect_source, ns.runtime_url, SOURCE, http_get=get, now=NOW)
        try:
            assert fetching.wait(5)
            assert maintenance(ns.migration_url, timeout_seconds=0.05)["drained"] is False
        finally:
            resume.set()
        with pytest.raises(MaintenanceMode):
            future.result(timeout=5)
    assert maintenance(ns.migration_url)["drained"] is True
    with connect(ns.migration_url) as conn:
        with conn.cursor() as cursor:
            for table in ("watchlist_announcement", "watchlist_announcement_check_state", "watchlist_announcement_gap", "watchlist_announcement_run", "watchlist_announcement_recovery"):
                cursor.execute(f"SELECT count(*) FROM {table}")
                assert cursor.fetchone()[0] == 0


def test_maintenance_waits_for_model_and_discards_late_enhancement(active) -> None:
    ns, _ = active
    enhancing, resume = threading.Event(), threading.Event()

    def enhance(*_args):
        enhancing.set()
        assert resume.wait(5)
        return {"title_zh": "late result", "importance": "high"}

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collect_source, ns.runtime_url, SOURCE, http_get=lambda _url: FEED, now=NOW, enhancer=enhance)
        try:
            assert enhancing.wait(5)
            assert maintenance(ns.migration_url, timeout_seconds=0.05)["drained"] is False
        finally:
            resume.set()
        with pytest.raises(MaintenanceMode):
            future.result(timeout=5)
    assert maintenance(ns.migration_url)["drained"] is True
    with connect(ns.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT title, title_zh, importance FROM watchlist_announcement")
            assert cursor.fetchone() == ("Raw", None, None)
            cursor.execute("SELECT count(*) FROM watchlist_announcement_run")
            assert cursor.fetchone()[0] == 0


def test_maintenance_does_not_claim_drain_while_write_transaction_is_open(active) -> None:
    ns, _ = active
    with connect(ns.runtime_url) as writer:
        with writer.cursor() as cursor:
            business_write_guard(cursor)
        assert maintenance(ns.migration_url, timeout_seconds=0.05)["drained"] is False
    assert maintenance(ns.migration_url)["drained"] is True
