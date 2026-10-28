from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg2
from psycopg2.extras import RealDictCursor

MARKETS = ("US", "CN_HK", "CN_A")
CURRENCIES = {"US": "USD", "CN_HK": "HKD", "CN_A": "CNY"}
TIMEZONES = {"US": "America/New_York", "CN_HK": "Asia/Hong_Kong", "CN_A": "Asia/Shanghai"}


class WatchlistError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def canonical_code(market: str, code: str) -> str:
    value = code.strip().upper()
    if market == "CN_HK":
        match = re.fullmatch(r"(\d{1,5})(?:\.HK)?", value)
        if match:
            return match.group(1).zfill(5)
    elif market == "CN_A":
        match = re.fullmatch(r"(\d{6})(?:\.(?:SH|SZ|SS))?", value)
        if match:
            return match.group(1)
    return value


def validate_security(security: dict[str, Any]) -> dict[str, Any]:
    market = security.get("market")
    if market not in MARKETS:
        raise ValueError("不支持的市场")
    result = dict(security)
    result["stock_code"] = canonical_code(market, str(security.get("stock_code", "")))
    code = result["stock_code"]
    valid = {
        "US": re.fullmatch(r"[A-Z0-9]+([.-][A-Z0-9]+)*", code),
        "CN_HK": re.fullmatch(r"\d{5}", code),
        "CN_A": re.fullmatch(r"\d{6}", code),
    }[market]
    if not valid:
        raise ValueError("证券代码不符合市场规范")
    if not str(security.get("stock_name", "")).strip():
        raise ValueError("证券名称不能为空")
    result["currency"] = CURRENCIES[market]
    if security.get("quote_source") != "tencent" or not str(security.get("quote_symbol", "")).strip():
        raise ValueError("必须显式配置有效的行情源与源代码")
    suffix = security.get("exchange_suffix")
    if market == "US" and suffix not in {".OQ", ".N"}:
        raise ValueError("美股必须显式配置 .OQ 或 .N 后缀")
    if market != "US" and suffix is not None:
        raise ValueError("非美股不需要交易所后缀")
    cik = security.get("cik")
    if cik is not None and str(cik).strip():
        digits = str(cik).strip()
        if not digits.isdigit() or len(digits) > 10:
            raise ValueError("CIK 必须是最多 10 位数字")
        result["cik"] = digits.zfill(10)
    else:
        result["cik"] = None
    return result


def number(value: Any) -> float | None:
    if value is None:
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0 else None


def summarize_quote(code: str, rows: list[dict], market_date: date) -> dict:
    rows = sorted(rows, key=lambda row: row["trade_date"])
    base = {
        "stock_code": code,
        "close": None,
        "change_pct": None,
        "quote_date": None,
        "previous_quote_date": None,
        "status": "missing",
        "warning": "没有日线行情",
        "trend": [],
        "stale": False,
        "price_basis": "raw_unadjusted",
    }
    if not rows:
        return base
    latest = rows[-1]
    close = number(latest["close"])
    previous = number(rows[-2]["close"]) if len(rows) > 1 else None
    quote_date = latest["trade_date"]
    base.update(
        close=close,
        quote_date=quote_date.isoformat(),
        previous_quote_date=rows[-2]["trade_date"].isoformat() if len(rows) > 1 else None,
        stale=(market_date - quote_date).days > 4,
        trend=[
            {"date": row["trade_date"].isoformat(), "close": number(row["close"])}
            for row in rows[-20:]
        ],
    )
    if close is None:
        base.update(status="invalid_close", warning="最近交易日收盘价缺失或无效")
    elif previous is None:
        base.update(status="missing_previous", warning="缺少有效前收价，无法计算涨跌幅")
    else:
        jumps = any(
            abs(number(second["close"]) / number(first["close"]) - 1) > 0.35
            for first, second in zip(rows, rows[1:])
            if number(first["close"]) is not None and number(second["close"]) is not None
        )
        if jumps:
            base.update(
                status="review_price_basis",
                trend=[],
                warning="窗口内价格跳变超过35%，需核对拆股/除权或真实异动",
            )
        else:
            base.update(
                status="ok",
                change_pct=(close / previous - 1) * 100,
                warning="原始日线价格，未校验拆股及除权",
            )
    return base


def list_items(url: str) -> list[dict]:
    with psycopg2.connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT w.id, s.market, s.stock_code, s.stock_name, s.currency,
                       s.quote_source, w.group_name, w.note, w.research_url,
                       w.sort_order, w.created_at, w.updated_at
                FROM watchlist_items w JOIN securities s ON s.id = w.security_id
                WHERE w.enabled = true
                ORDER BY w.sort_order, w.id
                """
            )
            rows = [dict(row) for row in cursor.fetchall()]
    for row in rows:
        row["created_at"] = row["created_at"].isoformat()
        row["updated_at"] = row["updated_at"].isoformat()
    return rows


def update_item(url: str, item_id: int, values: dict) -> dict:
    with psycopg2.connect(url) as conn:
        with conn.cursor() as cursor:
            from ..migration.common import BundleError
            from ..migration.service import business_write_guard

            try:
                business_write_guard(cursor)
            except BundleError as exc:
                raise WatchlistError(503, "应用处于维护状态，暂停业务写入") from exc
            cursor.execute(
                """
                UPDATE watchlist_items SET group_name = %s, note = %s,
                    research_url = %s, sort_order = %s, updated_at = now()
                WHERE id = %s AND enabled = true RETURNING id
                """,
                (
                    values["group_name"],
                    values["note"],
                    values["research_url"],
                    values["sort_order"],
                    item_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise WatchlistError(404, "关注项不存在或已停用")
    return {"id": row[0]}


def quote_batch(url: str, market: str, codes: list[str], now: datetime | None = None) -> dict:
    if market not in MARKETS:
        raise WatchlistError(422, "不支持的市场")
    canonical = {code: canonical_code(market, code) for code in codes}
    current = now or datetime.now(timezone.utc)
    market_date = current.astimezone(ZoneInfo(TIMEZONES[market])).date()
    quotes = []
    with psycopg2.connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            for requested in codes:
                code = canonical[requested]
                cursor.execute(
                    """
                    SELECT q.trade_date, q.close FROM daily_quotes q
                    JOIN securities s ON s.market = q.market AND s.stock_code = q.stock_code
                    JOIN watchlist_items w ON w.security_id = s.id AND w.enabled = true
                    WHERE q.market = %s AND q.stock_code = %s
                    ORDER BY q.trade_date DESC LIMIT 20
                    """,
                    (market, code),
                )
                rows = [dict(row) for row in reversed(cursor.fetchall())]
                quotes.append(summarize_quote(requested, rows, market_date))
    return {
        "market": market,
        "currency": CURRENCIES[market],
        "source": "database",
        "price_basis": "raw_unadjusted",
        "fetched_at": current.isoformat(),
        "quotes": quotes,
    }
