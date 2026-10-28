from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest

from hl_app.collectors.announcements import AnnouncementRecord, record_attempt, upsert_announcement
from hl_app.collectors.sources import collect_source
from hl_app.db import migrate
from hl_app.services import announcements, news

from .conftest import PostgresNamespace

pytestmark = pytest.mark.db
NOW = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def seeded(postgres_namespace: PostgresNamespace, monkeypatch):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    monkeypatch.setenv("HL_APP_DATABASE_URL", postgres_namespace.runtime_url)
    monkeypatch.setenv("HL_APP_COLLECTORS_ENABLED", "true")
    monkeypatch.setattr(news, "datetime", FixedDateTime)
    monkeypatch.setattr(announcements, "datetime", FixedDateTime)
    with psycopg2.connect(postgres_namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            for code, name in [("MU", "First"), ("WDC", "Second"), ("00001", "HK")]:
                market = "US" if code != "00001" else "CN_HK"
                currency = "USD" if market == "US" else "HKD"
                cik = "0000001234" if market == "US" else None
                symbol = f"{code}.OQ" if market == "US" else f"hk{code}"
                suffix = ".OQ" if market == "US" else None
                cursor.execute(
                    "INSERT INTO securities (market, stock_code, stock_name, currency, quote_source, quote_symbol, exchange_suffix, cik) VALUES (%s, %s, %s, %s, 'tencent', %s, %s, %s) RETURNING id",
                    (market, code, name, currency, symbol, suffix, cik),
                )
                cursor.execute("INSERT INTO watchlist_items (security_id) VALUES (%s)", (cursor.fetchone()[0],))
            cursor.execute(
                "INSERT INTO watchlist_announcement_source (source_id, market, source_kind, issuer_identifier, allowed_domain, entry_url, enabled, source_status) VALUES (1, 'US', 'media', '', 'example.com', 'https://example.com/feed', true, 'enabled')"
            )
    return postgres_namespace


def record(record_id="stable-1", title="Shared - Reuters", content_hash="one"):
    return AnnouncementRecord(
        source_record_id=record_id,
        form_type="media_report",
        title=title,
        url=f"https://example.com/{record_id}",
        published_at=NOW - timedelta(hours=2),
        published_precision="second",
        published_raw="2026-10-22T10:00:00Z",
        source_timezone="UTC",
        content_hash=content_hash,
        content_nature="unclassified",
        media_source="Reuters",
        importance="high",
    )


def test_revision_and_multi_company_association(seeded) -> None:
    with psycopg2.connect(seeded.migration_url) as conn:
        first = upsert_announcement(conn, 1, "", [("MU", "ticker MU"), ("WDC", "ticker WDC")], record(), False, NOW)
        second = upsert_announcement(conn, 1, "", [("MU", "ticker MU"), ("WDC", "ticker WDC")], record(content_hash="two"), False, NOW + timedelta(minutes=1))
        assert first == ("inserted", 1) and second == ("revised", 2)
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM watchlist_announcement")
            assert cursor.fetchone()[0] == 2
            cursor.execute("SELECT market, stock_code, relation_reason FROM watchlist_announcement_security ORDER BY stock_code")
            assert cursor.fetchall() == [("US", "MU", "ticker MU"), ("US", "WDC", "ticker WDC")]
    feed = news.feed("2026-10-22")
    assert feed["entries"][0]["stock_codes"] == ["MU", "WDC"]
    relations = feed["entries"][0]["stock_relations"]
    assert relations[0]["reasons"][0]["text"] == "ticker MU"
    assert relations[1]["reasons"][0]["text"] == "ticker WDC"


def test_disabled_security_gets_no_new_association(seeded) -> None:
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE watchlist_items SET enabled = false WHERE security_id = (SELECT id FROM securities WHERE stock_code = 'MU')")
        upsert_announcement(conn, 1, "", [("MU", "ticker MU"), ("WDC", "ticker WDC")], record(), False, NOW)
        with conn.cursor() as cursor:
            cursor.execute("SELECT stock_code FROM watchlist_announcement_security")
            assert cursor.fetchall() == [("WDC",)]


def test_media_collect_failure_leaves_gap_and_visible_source_state(seeded) -> None:
    source = {
        "source_id": 1,
        "source_kind": "media",
        "issuer_identifier": "",
        "entry_url": "https://example.com/feed",
        "parser_version": 1,
        "interval_minutes": 60,
    }

    def fail(_url):
        raise RuntimeError("fixture network failed")

    with pytest.raises(RuntimeError, match="fixture network failed"):
        collect_source(seeded.runtime_url, source, http_get=fail, now=NOW)
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT last_attempt_status, last_error_class FROM watchlist_announcement_check_state WHERE source_id = 1")
            assert cursor.fetchone() == ("failed", "RuntimeError")
            cursor.execute("SELECT COUNT(*) FROM watchlist_announcement_gap WHERE source_id = 1 AND resolved_at IS NULL")
            assert cursor.fetchone()[0] == 1
    feed = news.feed("2026-10-22")
    assert feed["sources"][0]["state"] == "failed"
    assert feed["sources"][0]["open_gaps"] == 1


def test_summary_marks_non_us_unsupported(seeded) -> None:
    result = announcements.summary(7)
    hk = next(item for item in result["items"] if item["market"] == "CN_HK")
    assert hk["sources"][0]["state"] == "unsupported_market"


def test_pinned_uses_high_and_multi_source(seeded) -> None:
    with psycopg2.connect(seeded.migration_url) as conn:
        upsert_announcement(conn, 1, "", [("MU", "ticker MU")], record(), False, NOW)
    result = news.pinned()
    assert result["entries"][0]["importance"] == "high"


def test_sec_budget_preserves_checkpoint_and_recovers_next_page(seeded) -> None:
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO watchlist_announcement_source (source_id, market, source_kind, issuer_identifier, allowed_domain, entry_url, enabled, source_status) VALUES (2, 'US', 'sec', '0000001234', 'data.sec.gov', 'https://data.sec.gov/submissions/CIK0000001234.json', true, 'enabled')"
            )
    calls = []

    class FakeSEC:
        def get(self, url):
            calls.append(url)
            if "CIK0000001234.json" in url:
                return json.dumps({
                    "filings": {
                        "recent": {key: [] for key in ["accessionNumber", "form", "acceptanceDateTime", "filingDate", "primaryDocument", "primaryDocDescription"]},
                        "files": [
                            {"name": "old-1.json", "filingFrom": "2026-01-01"},
                            {"name": "old-2.json", "filingFrom": "2026-02-01"},
                            {"name": "old-3.json", "filingFrom": "2026-03-01"},
                        ],
                    }
                }).encode()
            index = int(url.rsplit("-", 1)[1][0])
            return json.dumps({
                "filings": {"recent": {
                    "accessionNumber": [f"0001-26-00000{index}"],
                    "form": ["8-K"],
                    "acceptanceDateTime": ["2026-10-20T12:00:00Z"],
                    "filingDate": ["2026-10-20"],
                    "primaryDocument": ["a.htm"],
                    "primaryDocDescription": ["Current report"],
                }}
            }).encode()

    source = {
        "source_id": 2,
        "source_kind": "sec",
        "issuer_identifier": "0000001234",
        "parser_version": 1,
        "interval_minutes": 60,
    }
    first = collect_source(seeded.runtime_url, source, sec_client=FakeSEC(), now=NOW)
    assert first["partial"] is True
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT checked_through FROM watchlist_announcement_check_state WHERE source_id = 2")
            assert cursor.fetchone()[0] is None
            cursor.execute("SELECT cursor FROM watchlist_announcement_recovery WHERE source_id = 2")
            assert cursor.fetchone()[0] == "2"
    second = collect_source(seeded.runtime_url, source, sec_client=FakeSEC(), now=NOW + timedelta(minutes=1))
    assert second["partial"] is False
    assert calls[-1].endswith("old-3.json")
    with psycopg2.connect(seeded.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT checked_through FROM watchlist_announcement_check_state WHERE source_id = 2")
            assert cursor.fetchone()[0] is not None
            cursor.execute("SELECT COUNT(*) FROM watchlist_announcement_recovery WHERE source_id = 2")
            assert cursor.fetchone()[0] == 0
