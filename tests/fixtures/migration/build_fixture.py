from __future__ import annotations

from pathlib import Path

from hl_app.migration.common import write_bundle

ROOT = Path(__file__).resolve().parent / "full"
NOW = "2026-10-22T12:00:00+00:00"

rows = {name: [] for name in (
    "securities", "watchlist_items", "daily_quotes", "watchlist_announcement_source",
    "watchlist_announcement", "watchlist_announcement_security",
    "watchlist_announcement_check_state", "watchlist_announcement_gap",
    "watchlist_announcement_run", "watchlist_announcement_recovery",
)}
rows["securities"] = [
    {"id": 1, "market": "US", "stock_code": "MU", "stock_name": "Micron", "currency": "USD", "quote_source": "tencent", "quote_symbol": "MU.OQ", "exchange_suffix": ".OQ", "cik": "0000001234", "industry": "半导体", "created_at": NOW, "updated_at": NOW},
    {"id": 2, "market": "US", "stock_code": "WDC", "stock_name": "Western Digital", "currency": "USD", "quote_source": "tencent", "quote_symbol": "WDC.OQ", "exchange_suffix": ".OQ", "cik": "0000001234", "industry": "存储", "created_at": NOW, "updated_at": NOW},
]
rows["watchlist_items"] = [
    {"id": 1, "security_id": 1, "group_name": "研究", "note": "初始理由", "research_url": "https://example.com/mu", "sort_order": 0, "enabled": True, "created_at": NOW, "updated_at": NOW},
    {"id": 2, "security_id": 2, "group_name": "", "note": "", "research_url": "", "sort_order": 1, "enabled": True, "created_at": NOW, "updated_at": NOW},
]
rows["watchlist_announcement_source"] = [
    {"source_id": 1, "market": "US", "source_kind": "media", "platform": "fixture_media", "issuer_identifier": "", "allowed_domain": "example.com", "entry_url": "https://example.com/feed", "source_timezone": "UTC", "parser_version": 1, "interval_minutes": 60, "enabled": False, "source_status": "blocked", "status_reason": "fixture blocked", "created_at": NOW, "updated_at": NOW, "query_config": "{\"relations\":[]}"},
    {"source_id": 2, "market": "US", "source_kind": "sec", "platform": None, "issuer_identifier": "0000001234", "allowed_domain": "data.sec.gov", "entry_url": "https://data.sec.gov/submissions/CIK0000001234.json", "source_timezone": "UTC", "parser_version": 1, "interval_minutes": 60, "enabled": False, "source_status": "pending_verification", "status_reason": "fixture pending", "created_at": NOW, "updated_at": NOW, "query_config": None},
]
rows["watchlist_announcement"] = [
    {"id": 1, "market": "US", "issuer_identifier": "", "source_id": 1, "source_record_id": "story-1", "title": "Shared - Reuters", "form_type": "media_report", "original_url": "https://example.com/story-1", "published_at": None, "published_at_precision": "unknown", "source_published_raw": "", "source_timezone": "UTC", "first_observed_at": NOW, "last_seen_at": NOW, "captured_at": NOW, "content_hash": "one", "excerpt": "raw", "content_nature": "unclassified", "relation_reason": None, "media_source": "Reuters", "revision": 1, "is_backfill": True, "title_zh": None, "summary_zh": None, "importance": "high"},
    {"id": 2, "market": "US", "issuer_identifier": "", "source_id": 1, "source_record_id": "story-1", "title": "Shared updated - Reuters", "form_type": "media_report", "original_url": "https://example.com/story-1", "published_at": "2026-10-22T10:00:00+00:00", "published_at_precision": "second", "source_published_raw": "2026-10-22T10:00:00Z", "source_timezone": "UTC", "first_observed_at": NOW, "last_seen_at": NOW, "captured_at": NOW, "content_hash": "two", "excerpt": "raw v2", "content_nature": "unclassified", "relation_reason": None, "media_source": "Reuters", "revision": 2, "is_backfill": False, "title_zh": "共享更新", "summary_zh": "摘要", "importance": "high"},
]
rows["watchlist_announcement_security"] = [
    {"announcement_id": 1, "market": "US", "stock_code": "MU", "created_at": NOW, "relation_reason": "ticker MU"},
    {"announcement_id": 1, "market": "US", "stock_code": "WDC", "created_at": NOW, "relation_reason": "ticker WDC"},
]
rows["watchlist_announcement_check_state"] = [
    {"market": "US", "issuer_identifier": "", "source_id": 1, "effective_interval_minutes": 60, "last_attempt_at": NOW, "last_attempt_status": "failed", "last_success_at": None, "next_due_at": NOW, "checked_through": None, "covered_ranges": [], "last_error_class": "fixture_error", "updated_at": NOW},
]
rows["watchlist_announcement_gap"] = [
    {"id": 1, "market": "US", "issuer_identifier": "", "source_id": 1, "gap_range": "[2026-10-20 00:00:00+00,2026-10-21 00:00:00+00)", "reason": "fetch_failed", "detail": "fixture", "created_at": NOW, "resolved_at": None},
]
rows["watchlist_announcement_run"] = [
    {"id": 1, "task_id": "11111111-1111-1111-1111-111111111111", "market": "US", "issuer_identifier": "", "source_id": 1, "started_at": NOW, "finished_at": None, "status": "partial", "fetched_count": 2, "inserted_count": 1, "revised_count": 1, "error_class": "interrupted", "error_detail": "fixture recovery", "parser_version": 1, "dropped_count": 0, "trigger_kind": "recovery"},
]
rows["watchlist_announcement_recovery"] = [
    {"market": "US", "issuer_identifier": "", "source_id": 1, "backfill_start": "2026-10-01T00:00:00+00:00", "target_through": NOW, "completed_ranges": [], "pending_ranges": ["[2026-10-20 00:00:00+00,2026-10-21 00:00:00+00)"], "cursor": "2", "cursor_applicable": True, "updated_at": NOW},
]
manifest = {
    "bundle_id": "fixture-full-v1",
    "bundle_version": 1,
    "kind": "full",
    "direction": "stock_data_to_hl",
    "source_instance": "fixture-source",
    "source_commit": "fixture-commit",
    "source_schema": 1,
    "scope": {"markets": ["US"], "securities": ["MU", "WDC"]},
    "cutoff_at": NOW,
    "snapshot_id": "fixture-snapshot-1",
    "quiesced": True,
    "baseline_id": None,
    "baseline_hash": None,
}

if __name__ == "__main__":
    write_bundle(ROOT, manifest, rows)
