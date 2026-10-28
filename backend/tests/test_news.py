from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hl_app.collectors.sources import parse_feed, parse_sec
from hl_app.services.news import (
    _ai_available,
    _media_group_entry,
    _merge_by_announcement,
    _within_48h_pinned,
    fold_media,
    normalize_title,
)


def media_row(id: int, title: str, published: datetime | None, source: str = "Reuters", code: str = "MU", reason: str | None = None):
    return {
        "id": id,
        "source_id": 1,
        "source_record_id": f"r{id}",
        "stock_code": code,
        "title": title,
        "original_url": f"https://example.com/{id}",
        "source_kind": "media",
        "platform": "feed",
        "media_source": source,
        "form_type": "media_report",
        "content_nature": "unclassified",
        "relation_reason": None,
        "association_reason": reason,
        "excerpt": None,
        "published_at": published,
        "published_at_precision": "second" if published else "unknown",
        "source_published_raw": "",
        "source_timezone": "UTC",
        "time_unknown": published is None,
        "first_observed_at": published or datetime(2026, 10, 20, tzinfo=timezone.utc),
        "is_backfill": False,
        "title_zh": None,
        "summary_zh": None,
        "importance": None,
    }


def test_title_normalization_only_removes_known_publisher() -> None:
    assert normalize_title("Company beats - Reuters", "Reuters") == "company beats"
    assert normalize_title("Company raised - lowered guidance", "Reuters") == "company raised lowered guidance"


def test_media_fold_48h_boundary_and_unknown_time() -> None:
    start = datetime(2026, 10, 20, tzinfo=timezone.utc)
    rows = [
        media_row(1, "Same - Reuters", start),
        media_row(2, "Same - CNBC", start + timedelta(hours=48), "CNBC"),
        media_row(3, "Same - Bloomberg", start + timedelta(hours=48, seconds=1), "Bloomberg"),
        media_row(4, "Same - Reuters", None),
    ]
    clusters = fold_media(rows)
    assert sorted(len(cluster) for cluster in clusters) == [1, 1, 2]
    unknown = next(cluster for cluster in clusters if cluster[0]["id"] == 4)
    assert len(unknown) == 1


def test_multi_company_reasons_stay_with_their_company() -> None:
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    rows = [
        media_row(1, "Shared", now, code="MU", reason="ticker MU"),
        media_row(1, "Shared", now, code="WDC", reason="ticker WDC"),
    ]
    merged = _merge_by_announcement(rows)[0]
    assert merged["stock_codes"] == ["MU", "WDC"]
    assert merged["_relations"]["MU"][0]["text"] == "ticker MU"
    assert merged["_relations"]["WDC"][0]["text"] == "ticker WDC"
    entry = _media_group_entry([merged])
    assert entry["stock_codes"] == ["MU", "WDC"]


def test_pinned_window_rules_and_ai_availability() -> None:
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    assert _within_48h_pinned(media_row(1, "x", now - timedelta(hours=48)), now)
    assert not _within_48h_pinned(media_row(2, "x", now - timedelta(hours=48, seconds=1)), now)
    day_row = media_row(3, "x", None)
    day_row["published_at_precision"] = "day"
    day_row["source_published_raw"] = "2026-10-21"
    assert _within_48h_pinned(day_row, now)
    assert not _within_48h_pinned(media_row(4, "x", None), now)
    recent = media_row(5, "x", now)
    assert not _ai_available([recent], now)
    recent["importance"] = "high"
    assert _ai_available([recent], now)


def test_feed_parser_preserves_unknown_time() -> None:
    payload = b"""<rss><channel><item><title>Example</title><link>https://example.com/a</link><guid>stable-a</guid><description>text</description></item></channel></rss>"""
    source = {"source_kind": "media", "source_timezone": "UTC", "platform": "fixture"}
    records = parse_feed(payload, source)
    assert len(records) == 1
    assert records[0].published_at is None and records[0].published_precision == "unknown"
    assert records[0].source_record_id == "stable-a"


def test_sec_parser_uses_acceptance_time_and_day_precision() -> None:
    source = {"issuer_identifier": "0000001234"}
    payload = b'{"filings":{"recent":{"accessionNumber":["0001-26-000001","0001-26-000002"],"form":["8-K","10-Q"],"acceptanceDateTime":["2026-10-20T12:00:00Z",""],"filingDate":["2026-10-20","2026-10-19"],"primaryDocument":["a.htm","b.htm"],"primaryDocDescription":["Current report","Quarterly report"]}}}'
    records = parse_sec(payload, source)
    assert records[0].published_precision == "second"
    assert records[1].published_at is None and records[1].published_precision == "day"
    assert records[0].source_record_id == "0001-26-000001"
