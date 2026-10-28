from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import RealDictCursor

from ..services.watchlist import TIMEZONES

TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
CLOSE_TIMES = {"US": time(16, 0), "CN_HK": time(16, 0), "CN_A": time(15, 0)}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _default_get(url: str, params: dict[str, str]) -> dict:
    request = Request(
        f"{url}?{urlencode(params)}", headers={"User-Agent": "hl-app-quote-collector/1"}
    )
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def fetch_tencent_hist(
    security: dict,
    start_date: date,
    end_date: date,
    http_get: Callable[[str, dict[str, str]], dict] = _default_get,
) -> list[dict]:
    market = security["market"]
    if market not in TIMEZONES:
        raise ValueError("不支持的市场")
    symbol = security["quote_symbol"]
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("缺少显式行情源代码")
    records: dict[str, dict] = {}
    segment_end = end_date
    for _ in range(2):
        payload = http_get(
            TENCENT_KLINE_URL,
            {"param": f"{symbol},day,{start_date.isoformat()},{segment_end.isoformat()},800,"},
        )
        day_data: list = []
        raw = payload.get("data", {})
        if isinstance(raw, dict):
            for value in raw.values():
                if isinstance(value, dict) and value.get("day"):
                    day_data = value["day"]
                    break
        if not day_data:
            break
        for row in day_data:
            if not isinstance(row, list) or len(row) < 6:
                continue
            try:
                trade_date = date.fromisoformat(row[0])
            except (TypeError, ValueError):
                continue
            if start_date <= trade_date <= end_date:
                records[trade_date.isoformat()] = {
                    "market": market,
                    "stock_code": security["stock_code"],
                    "trade_date": trade_date,
                    "open": _number(row[1]),
                    "high": _number(row[3]),
                    "low": _number(row[4]),
                    "close": _number(row[2]),
                    "volume": int(_number(row[5])) if _number(row[5]) is not None and _number(row[5]) >= 0 else None,
                    "source": "tencent",
                    "currency": security["currency"],
                    "price_basis": "raw_unadjusted",
                }
        earliest = min(date.fromisoformat(row[0]) for row in day_data)
        if len(day_data) < 800 or earliest <= start_date:
            break
        segment_end = earliest - timedelta(days=1)
    return [records[key] for key in sorted(records)]


def closed_records(records: list[dict], market: str, now: datetime) -> list[dict]:
    local_now = now.astimezone(ZoneInfo(TIMEZONES[market]))
    result = []
    for record in records:
        trade_date = record["trade_date"]
        if trade_date < local_now.date() or (
            trade_date == local_now.date() and local_now.time() >= CLOSE_TIMES[market]
        ):
            result.append(record)
    return result


def enabled_securities(url: str) -> list[dict]:
    with psycopg2.connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT s.* FROM securities s JOIN watchlist_items w ON w.security_id = s.id
                WHERE w.enabled = true ORDER BY s.market, s.stock_code
                """
            )
            return [dict(row) for row in cursor.fetchall()]


def collect_security(
    url: str,
    security: dict,
    now: datetime | None = None,
    http_get: Callable[[str, dict[str, str]], dict] = _default_get,
) -> int:
    with psycopg2.connect(url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT enabled FROM watchlist_items WHERE security_id = %s", (security["id"],))
            enabled = cursor.fetchone()
    if enabled is None or not enabled[0]:
        return 0
    current = now or datetime.now(timezone.utc)
    start = current.date() - timedelta(days=90)
    records = fetch_tencent_hist(security, start, current.date(), http_get)
    if len([row for row in records if _number(row["close"]) is not None]) < 20:
        records = fetch_tencent_hist(security, current.date() - timedelta(days=365), current.date(), http_get)
    records = closed_records(records, security["market"], current)
    with psycopg2.connect(url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT enabled FROM watchlist_items WHERE security_id = %s FOR UPDATE",
                (security["id"],),
            )
            row = cursor.fetchone()
            if row is None or not row[0]:
                return 0
            for record in records:
                cursor.execute(
                    """
                    INSERT INTO daily_quotes (
                        market, stock_code, trade_date, open, high, low, close,
                        volume, source, currency, price_basis, collected_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (market, stock_code, trade_date) DO UPDATE SET
                        open = COALESCE(daily_quotes.open, EXCLUDED.open),
                        high = COALESCE(daily_quotes.high, EXCLUDED.high),
                        low = COALESCE(daily_quotes.low, EXCLUDED.low),
                        close = COALESCE(daily_quotes.close, EXCLUDED.close),
                        volume = COALESCE(daily_quotes.volume, EXCLUDED.volume),
                        source = EXCLUDED.source,
                        collected_at = EXCLUDED.collected_at
                    """,
                    (
                        record["market"], record["stock_code"], record["trade_date"],
                        record["open"], record["high"], record["low"], record["close"],
                        record["volume"], record["source"], record["currency"], record["price_basis"],
                    ),
                )
    return len(records)
