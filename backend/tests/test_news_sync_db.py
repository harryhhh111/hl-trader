from __future__ import annotations

import json
from pathlib import Path

import psycopg2
import pytest

from hl_app.db import migrate
from hl_app.services import news, news_sync
from hl_app.services import news_sync_store as store

from .conftest import PostgresNamespace

pytestmark = pytest.mark.db
FIXTURE = Path(__file__).parents[2] / "tests/fixtures/integrations/hl_trader_news_sync_v1.json"


@pytest.fixture
def ready(postgres_namespace: PostgresNamespace, monkeypatch):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    monkeypatch.setenv("HL_APP_DATABASE_URL", postgres_namespace.runtime_url)
    monkeypatch.setenv("HL_APP_COLLECTORS_ENABLED", "false")
    return postgres_namespace


def response() -> news_sync.NewsSyncResponse:
    return news_sync.parse_response(FIXTURE.read_bytes())


def seed_watchlist(namespace: PostgresNamespace) -> None:
    with psycopg2.connect(namespace.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO securities (market, stock_code, stock_name, currency, quote_source, quote_symbol, exchange_suffix) VALUES ('US', 'ORCL', 'Oracle', 'USD', 'tencent', 'ORCL.N', '.N') RETURNING id"
            )
            security_id = cursor.fetchone()[0]
            cursor.execute("INSERT INTO watchlist_items (security_id) VALUES (%s)", (security_id,))


def test_bootstrap_apply_activate_replay_and_existing_news_page(ready) -> None:
    seed_watchlist(ready)
    result = store.apply_page(ready.runtime_url, response(), "stock-data-fixture", bootstrap=True)
    assert result["bootstrap_status"] == "completed" and result["events"] == 4
    status = store.status(ready.runtime_url)
    assert status["counts"]["sources"] == 1
    assert status["counts"]["logical_announcements"] == 1
    assert status["counts"]["revisions"] == 1
    with psycopg2.connect(ready.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT revision, title_zh, summary_zh, importance FROM watchlist_announcement")
            assert cursor.fetchone() == (2, "共享测试资讯", "中文摘要", "high")
            cursor.execute("SELECT market, stock_code FROM watchlist_announcement_security ORDER BY stock_code")
            assert cursor.fetchall() == [("US", "NBIS"), ("US", "ORCL")]
            cursor.execute("SELECT count(*) FROM watchlist_items")
            assert cursor.fetchone()[0] == 1
            cursor.execute("SELECT open_gaps FROM hl_news_sync_source_state")
            assert cursor.fetchone()[0] == 1
    assert store.activate(ready.migration_url)["status"] == "active"

    replay = store.apply_page(ready.runtime_url, response(), "stock-data-fixture")
    assert replay["replayed"] == 4
    assert store.status(ready.runtime_url)["counts"]["revisions"] == 1
    feed = news.feed("2026-11-12")
    assert feed["entries"][0]["title_zh"] == "共享测试资讯"
    assert feed["sources"][0]["state"] == "blocked"
    assert feed["sources"][0]["open_gaps"] == 1
    with psycopg2.connect(ready.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE watchlist_announcement_source SET enabled = true, source_status = 'enabled'")
    from hl_app.collectors.sources import registered_sources
    assert registered_sources(ready.runtime_url) == []


def test_bootstrap_can_resume_from_committed_page(ready) -> None:
    first_value = json.loads(FIXTURE.read_text())
    first_value["items"] = first_value["items"][:1]
    first_value["next_cursor"] = "page-2"
    first_value["has_more"] = True
    first = news_sync.parse_response(json.dumps(first_value).encode())
    result = store.apply_page(ready.runtime_url, first, "stock-data-fixture", bootstrap=True)
    assert result["bootstrap_status"] == "in_progress"
    position = news_sync.sync_position(ready.runtime_url)
    assert position["next_cursor"] == "page-2"
    second_value = json.loads(FIXTURE.read_text())
    second_value["sources"] = []
    second_value["items"] = second_value["items"][1:]
    second = news_sync.parse_response(json.dumps(second_value).encode())
    result = store.apply_page(ready.runtime_url, second, "stock-data-fixture", bootstrap=True)
    assert result["bootstrap_status"] == "completed"
    assert store.status(ready.runtime_url)["counts"]["revisions"] == 1


def test_page_failure_rolls_back_events_receipts_and_cursor(ready) -> None:
    store.apply_page(ready.runtime_url, response(), "stock-data-fixture", bootstrap=True)
    store.activate(ready.migration_url)
    value = json.loads(FIXTURE.read_text())
    value["sources"] = []
    new_item = dict(value["items"][0])
    new_item.update(sync_seq=5, source_record_id="story-2", revision=1)
    missing_source = dict(value["items"][1])
    missing_source.update(sync_seq=6, source_id=999, source_record_id="story-3", revision=1)
    value["items"] = [new_item, missing_source]
    value["next_cursor"] = "must-not-advance"
    with pytest.raises(store.SyncStoreError, match="来源映射"):
        store.apply_page(ready.runtime_url, news_sync.parse_response(json.dumps(value).encode()), "stock-data-fixture")
    assert news_sync.sync_position(ready.runtime_url)["next_cursor"] == "opaque-cursor-4"
    with psycopg2.connect(ready.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM watchlist_announcement WHERE source_record_id = 'story-2'")
            assert cursor.fetchone()[0] == 0
            cursor.execute("SELECT count(*) FROM hl_news_sync_event_receipt WHERE sync_seq >= 5")
            assert cursor.fetchone()[0] == 0


def test_same_sequence_with_different_payload_is_rejected(ready) -> None:
    store.apply_page(ready.runtime_url, response(), "stock-data-fixture", bootstrap=True)
    store.activate(ready.migration_url)
    value = json.loads(FIXTURE.read_text())
    value["items"][0]["title"] = "Conflict"
    with pytest.raises(store.SyncStoreError, match="冲突"):
        store.apply_page(ready.runtime_url, news_sync.parse_response(json.dumps(value).encode()), "stock-data-fixture")
    with psycopg2.connect(ready.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT title FROM watchlist_announcement")
            assert cursor.fetchone()[0] == "Shared fixture story"


def test_activation_rejects_business_change_after_bootstrap(ready) -> None:
    store.apply_page(ready.runtime_url, response(), "stock-data-fixture", bootstrap=True)
    with psycopg2.connect(ready.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE watchlist_announcement SET title = 'Changed after bootstrap'")
    with pytest.raises(store.SyncStoreError, match="业务数据已变化"):
        store.activate(ready.migration_url)


class FakeClient:
    source_instance = "stock-data-fixture"

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.cursors = []

    def fetch(self, cursor):
        self.cursors.append(cursor)
        return self.responses.pop(0)


def test_run_bootstrap_and_empty_incremental_page(ready) -> None:
    client = FakeClient([response()])
    result = news_sync.run(ready.runtime_url, bootstrap=True, client=client)
    assert result["events"] == 4 and result["status"] == "completed"
    store.activate(ready.migration_url)
    empty = json.loads(FIXTURE.read_text())
    empty["sources"] = []
    empty["items"] = []
    empty["next_cursor"] = "empty-page-cursor"
    incremental = FakeClient([news_sync.parse_response(json.dumps(empty).encode())])
    result = news_sync.run(ready.runtime_url, client=incremental)
    assert result["events"] == 0
    assert incremental.cursors == ["opaque-cursor-4"]
    assert news_sync.sync_position(ready.runtime_url)["next_cursor"] == "empty-page-cursor"


def test_incremental_appends_revision_and_keeps_minimum_relation_anchor(ready) -> None:
    store.apply_page(ready.runtime_url, response(), "stock-data-fixture", bootstrap=True)
    store.activate(ready.migration_url)
    value = json.loads(FIXTURE.read_text())
    value["sources"] = []
    revised = dict(value["items"][0])
    revised.update(sync_seq=5, revision=3, title="Updated fixture story")
    value["items"] = [revised]
    store.apply_page(ready.runtime_url, news_sync.parse_response(json.dumps(value).encode()), "stock-data-fixture")
    with psycopg2.connect(ready.runtime_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT count(*), max(revision) FROM watchlist_announcement")
            assert cursor.fetchone() == (2, 3)
            cursor.execute(
                "SELECT DISTINCT anchor.revision FROM watchlist_announcement_security r JOIN watchlist_announcement anchor ON anchor.id = r.announcement_id"
            )
            assert cursor.fetchall() == [(2,)]


def test_completed_sync_rejects_missing_history_and_instance_change(ready) -> None:
    store.apply_page(ready.runtime_url, response(), "stock-data-fixture", bootstrap=True)
    store.activate(ready.migration_url)
    value = json.loads(FIXTURE.read_text())
    value["sources"] = []
    missing = dict(value["items"][0])
    missing.update(sync_seq=5, source_record_id="new-story", revision=2)
    value["items"] = [missing]
    with pytest.raises(store.SyncStoreError, match="revision=1"):
        store.apply_page(ready.runtime_url, news_sync.parse_response(json.dumps(value).encode()), "stock-data-fixture")
    with pytest.raises(store.SyncStoreError, match="绑定不一致"):
        store.apply_page(ready.runtime_url, response(), "another-instance")


def test_empty_upstream_bootstrap_has_explicit_completed_state(ready) -> None:
    value = json.loads(FIXTURE.read_text())
    value["sources"] = []
    value["items"] = []
    result = store.apply_page(
        ready.runtime_url, news_sync.parse_response(json.dumps(value).encode()),
        "stock-data-fixture", bootstrap=True,
    )
    assert result["events"] == 0 and result["bootstrap_status"] == "completed"
    assert store.activate(ready.migration_url)["status"] == "active"
