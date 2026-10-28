from __future__ import annotations

import json

from hl_app.collectors.ir import parse_ir
from hl_app.collectors.sources import parse_finnhub


def test_nebius_adapter_only_accepts_press_releases() -> None:
    payload = json.dumps({
        "props": {
            "press_releases:{\"slug\":\"example\"}": {
                "name": "example",
                "title": "Example release",
                "date": "2026-10-20",
                "publication_datetime": "2026-10-20T10:00:00Z",
            },
            "blog_posts:{\"slug\":\"ignore\"}": {"name": "ignore", "title": "Ignore"},
        }
    }).encode()
    html = b'<script id="__NEXT_DATA__" type="application/json">' + payload + b"</script>"
    records = parse_ir(html, {"platform": "nebius_newsroom", "source_timezone": "UTC"})
    assert len(records) == 1
    assert records[0].source_record_id == "example"
    assert records[0].published_precision == "second"


def test_q4_adapter_preserves_day_and_unknown_time() -> None:
    payload = json.dumps({
        "GetPressReleaseListResult": [
            {"PressReleaseId": 1, "Title": "Day", "LinkToDetailPage": "/day", "PressReleaseDate": "2026-10-20"},
            {"PressReleaseId": 2, "Title": "Unknown", "LinkToDetailPage": "/unknown", "PressReleaseDate": ""},
        ]
    }).encode()
    records = parse_ir(payload, {"platform": "q4_press_release", "entry_url": "https://example.com/list", "source_timezone": "UTC"})
    assert [record.published_precision for record in records] == ["day", "unknown"]
    assert all(record.published_at is None for record in records)


def test_finnhub_adapter_keeps_source_and_summary() -> None:
    payload = json.dumps([{
        "id": "fixture-1",
        "headline": "Example",
        "datetime": 1792600000,
        "url": "https://example.com/story",
        "summary": "Summary",
        "source": "Reuters",
    }]).encode()
    records = parse_finnhub(payload, {"platform": "finnhub"})
    assert records[0].media_source == "Reuters"
    assert records[0].excerpt == "Summary"
    assert records[0].published_precision == "second"
