from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hl_app.auth import require_session
from hl_app.collectors.quotes import closed_records, fetch_tencent_hist
from hl_app.routes import watchlist
from hl_app.services.watchlist import canonical_code, summarize_quote, validate_security


def quotes(*prices):
    return [
        {"trade_date": date(2026, 10, 1) + timedelta(days=index), "close": price}
        for index, price in enumerate(prices)
    ]


def test_daily_change_and_series() -> None:
    result = summarize_quote("ORCL", quotes(100, 102), date(2026, 10, 2))
    assert result["change_pct"] == pytest.approx(2)
    assert result["close"] == 102
    assert result["previous_quote_date"] == "2026-10-01"
    assert result["price_basis"] == "raw_unadjusted"


@pytest.mark.parametrize("invalid", [None, 0, -1, float("nan"), float("inf")])
def test_invalid_latest_is_never_forward_filled(invalid) -> None:
    result = summarize_quote("ORCL", quotes(100, invalid), date(2026, 10, 2))
    assert result["close"] is None and result["change_pct"] is None
    assert result["status"] == "invalid_close"
    assert result["trend"][-1]["close"] is None


def test_missing_previous_does_not_jump_over_gap() -> None:
    result = summarize_quote("ORCL", quotes(100, None, 102), date(2026, 10, 3))
    assert result["status"] == "missing_previous"
    assert result["change_pct"] is None
    assert result["previous_quote_date"] == "2026-10-02"


def test_split_like_jump_is_not_normal_change() -> None:
    result = summarize_quote("ORCL", quotes(100, 50, 51), date(2026, 10, 3))
    assert result["status"] == "review_price_basis"
    assert result["change_pct"] is None and result["trend"] == []


def test_empty_short_and_stale_history() -> None:
    assert summarize_quote("ORCL", [], date(2026, 10, 2))["status"] == "missing"
    single = summarize_quote("ORCL", quotes(100), date(2026, 10, 8))
    assert single["status"] == "missing_previous" and single["stale"]


def test_canonical_code_formats() -> None:
    assert canonical_code("US", "aapl") == "AAPL"
    assert canonical_code("CN_HK", "2020.HK") == "02020"
    assert canonical_code("CN_HK", " 0700 ") == "00700"
    assert canonical_code("CN_A", "600519.SH") == "600519"
    assert canonical_code("CN_A", "000001.SZ") == "000001"


def test_security_validation_requires_explicit_source_and_cik() -> None:
    security = validate_security(
        {
            "market": "US",
            "stock_code": "brk-b",
            "stock_name": "Example",
            "quote_source": "tencent",
            "quote_symbol": "BRK.B.N",
            "exchange_suffix": ".N",
            "cik": "1234",
        }
    )
    assert security["stock_code"] == "BRK-B"
    assert security["currency"] == "USD" and security["cik"] == "0000001234"
    with pytest.raises(ValueError, match="后缀"):
        validate_security({**security, "exchange_suffix": None})
    with pytest.raises(ValueError, match="行情源"):
        validate_security({**security, "quote_symbol": ""})


def test_tencent_history_uses_explicit_class_share_symbol_and_filters_numbers() -> None:
    seen = []

    def fake_get(url, params):
        seen.append((url, params))
        return {
            "data": {
                "usBRK.B.N": {
                    "day": [
                        ["2026-10-01", "100", "101", "102", "99", "1000"],
                        ["2026-10-02", "101", "NaN", "103", "100", "2000"],
                    ]
                }
            }
        }

    records = fetch_tencent_hist(
        {
            "market": "US",
            "stock_code": "BRK-B",
            "quote_symbol": "BRK.B.N",
            "currency": "USD",
        },
        date(2026, 10, 1),
        date(2026, 10, 2),
        fake_get,
    )
    assert "BRK.B.N" in seen[0][1]["param"]
    assert records[0]["close"] == 101 and records[1]["close"] is None
    assert records[0]["price_basis"] == "raw_unadjusted"


@pytest.mark.parametrize(
    ("market", "current", "expected"),
    [
        ("US", datetime(2026, 10, 2, 19, 59, tzinfo=timezone.utc), 0),
        ("US", datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc), 1),
        ("CN_HK", datetime(2026, 10, 2, 7, 59, tzinfo=timezone.utc), 0),
        ("CN_HK", datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc), 1),
        ("CN_A", datetime(2026, 10, 2, 6, 59, tzinfo=timezone.utc), 0),
        ("CN_A", datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc), 1),
    ],
)
def test_only_closed_daily_bars_are_stored(market, current, expected) -> None:
    records = [{"trade_date": date(2026, 10, 2), "close": 100}]
    assert len(closed_records(records, market, current)) == expected


def test_watchlist_has_no_create_delete_and_patch_rejects_identity(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(watchlist.router, prefix="/api/v1")
    app.dependency_overrides[require_session] = lambda: None
    client = TestClient(app)
    assert client.post("/api/v1/watchlist", json={}).status_code == 405
    assert client.delete("/api/v1/watchlist/1").status_code == 405
    response = client.patch(
        "/api/v1/watchlist/1",
        json={"market": "US", "stock_code": "OTHER", "enabled": True},
    )
    assert response.status_code == 422
    assert client.post(
        "/api/v1/watchlist/quotes", json={"market": "US", "codes": ["bad/sql"]}
    ).status_code == 422
