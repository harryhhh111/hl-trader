from __future__ import annotations

from datetime import datetime, timezone

import psycopg2
import pytest

from hl_app.collectors.sources import collect_source
from hl_app.db import migrate
from hl_app.services.ai import suggest_fields
from hl_app.workers import WorkerAlreadyRunning, WorkerLifecycle, dry_run, run_once

from .conftest import PostgresNamespace
from .test_research import init_repo

pytestmark = pytest.mark.db
TOKEN = "fixture"


@pytest.fixture
def seeded(postgres_namespace: PostgresNamespace, monkeypatch, tmp_path):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    init_repo(tmp_path)
    monkeypatch.setenv("HL_APP_RESEARCH_REPO_PATH", str(tmp_path))
    with psycopg2.connect(postgres_namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO securities (market, stock_code, stock_name, currency, quote_source, quote_symbol, exchange_suffix, industry) VALUES ('US', 'ORCL', 'Oracle', 'USD', 'tencent', 'ORCL.N', '.N', '软件') RETURNING id"
            )
            cursor.execute("INSERT INTO watchlist_items (security_id, note) VALUES (%s, '人工理由')", (cursor.fetchone()[0],))
            cursor.execute(
                "INSERT INTO watchlist_announcement_source (source_id, market, source_kind, issuer_identifier, allowed_domain, entry_url, enabled, source_status) VALUES (1, 'US', 'media', '', 'example.com', 'https://example.com/feed', true, 'enabled')"
            )
    return postgres_namespace


def test_suggest_returns_draft_without_overwriting_saved_reason(seeded, monkeypatch) -> None:
    monkeypatch.setattr("hl_app.services.ai._run_mmx", lambda *_args: '{"group":"AI 云","note":"研究草稿"}')
    result = suggest_fields(seeded.runtime_url, "US", "ORCL", "Oracle")
    assert result["source"] == "research_doc" and result["note"] == "研究草稿"
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT note FROM watchlist_items")
            assert cursor.fetchone()[0] == "人工理由"


def test_no_research_entry_returns_marked_generic_draft(seeded, monkeypatch) -> None:
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO securities (market, stock_code, stock_name, currency, quote_source, quote_symbol, exchange_suffix) VALUES ('US', 'NBIS', 'Nebius', 'USD', 'tencent', 'NBIS.OQ', '.OQ') RETURNING id"
            )
            cursor.execute("INSERT INTO watchlist_items (security_id) VALUES (%s)", (cursor.fetchone()[0],))
    monkeypatch.setattr("hl_app.services.ai._run_mmx", lambda *_args: '{"group":"AI 云","note":"草稿"}')
    result = suggest_fields(seeded.runtime_url, "US", "NBIS", "Nebius")
    assert result["source"] == "generic"
    assert result["note"].startswith("[通用草稿]")


def test_reason_pool_allows_only_one_call(seeded, monkeypatch) -> None:
    import threading

    active = maximum = calls = 0
    guard = threading.Lock()

    def slow(*_args):
        nonlocal active, maximum, calls
        with guard:
            active += 1
            calls += 1
            maximum = max(maximum, active)
        threading.Event().wait(0.05)
        with guard:
            active -= 1
        return '{"group":"AI 云","note":"研究草稿"}'

    monkeypatch.setattr("hl_app.services.ai._run_mmx", slow)
    threads = [threading.Thread(target=suggest_fields, args=(seeded.runtime_url, "US", "ORCL", "Oracle")) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == 3 and maximum == 1


def test_enhancement_failure_keeps_raw_record(seeded) -> None:
    payload = b"<rss><channel><item><title>Raw</title><link>https://example.com/raw</link><guid>raw-1</guid></item></channel></rss>"
    source = {"source_id": 1, "source_kind": "media", "issuer_identifier": "", "entry_url": "https://example.com/feed", "parser_version": 1, "interval_minutes": 60, "query_config": '{"relations":[{"stock_code":"ORCL","reason":"fixture"}]}'}

    def fail(_record, _kind, _code):
        raise RuntimeError("model failed")

    result = collect_source(seeded.runtime_url, source, http_get=lambda _url: payload, now=datetime(2026, 10, 22, tzinfo=timezone.utc), enhancer=fail)
    assert result["enhancement_failed"] == 1 and result["inserted"] == 1
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT title, title_zh, importance FROM watchlist_announcement")
            assert cursor.fetchone() == ("Raw", None, None)
            cursor.execute("SELECT last_attempt_status, last_error_class FROM watchlist_announcement_check_state WHERE source_id = 1")
            assert cursor.fetchone() == ("partial", "enhance_failed")


def test_worker_lifecycle_is_exclusive_and_dry_run_has_no_network(seeded) -> None:
    first = WorkerLifecycle(seeded.runtime_url)
    second = WorkerLifecycle(seeded.runtime_url)
    first.acquire()
    try:
        with pytest.raises(WorkerAlreadyRunning):
            second.acquire()
        assert dry_run(seeded.runtime_url)["tasks"] == ["quotes", "announcements", "news_enhancement"]
    finally:
        first.close()
        second.close()


def test_disabled_once_does_no_collection(seeded, monkeypatch) -> None:
    monkeypatch.setenv("HL_APP_COLLECTORS_ENABLED", "false")
    assert run_once(seeded.runtime_url, "quotes") == {"status": "disabled", "kind": "quotes"}
