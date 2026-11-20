from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from psycopg2 import errors
from psycopg2.extras import RealDictCursor

from ..db import connect

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


def _write_guard(cursor) -> None:
    from ..migration.common import BundleError
    from ..migration.service import business_write_guard

    try:
        business_write_guard(cursor)
    except BundleError as exc:
        raise WatchlistError(503, "应用处于维护状态，暂停业务写入") from exc


def _queue_quote_collection(cursor, security_id: int) -> None:
    cursor.execute(
        """
        INSERT INTO quote_collection_state (security_id, status, next_attempt_at, updated_at)
        VALUES (%s, 'pending', now(), now())
        ON CONFLICT (security_id) DO UPDATE SET
            status = 'pending', last_error = NULL,
            next_attempt_at = now(), updated_at = now()
        """,
        (security_id,),
    )


def _public_row(row: dict) -> dict:
    for key, value in row.items():
        if isinstance(value, (date, datetime)):
            row[key] = value.isoformat()
    row["quote_status"] = row["quote_status"] or "pending"
    return row


def list_items(
    url: str,
    owner_account_id: int | None = None,
    include_disabled: bool = False,
) -> list[dict]:
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT w.id, w.enabled, s.market, s.stock_code, s.stock_name, s.currency,
                       s.quote_source, s.quote_symbol, s.exchange_suffix, s.cik,
                       w.group_name, w.note, w.research_url,
                       w.sort_order, w.created_at, w.updated_at,
                       q.status AS quote_status, q.last_attempt_at AS quote_last_attempt_at,
                       q.last_success_at AS quote_last_success_at,
                       q.latest_trade_date AS quote_latest_trade_date,
                       q.last_record_count AS quote_record_count,
                       q.last_error AS quote_error, q.next_attempt_at AS quote_next_attempt_at
                FROM watchlist_items w
                JOIN securities s ON s.id = w.security_id
                LEFT JOIN quote_collection_state q ON q.security_id = w.security_id
                WHERE w.owner_account_id IS NOT DISTINCT FROM %s
                  AND (%s OR w.enabled = true)
                ORDER BY w.enabled DESC, w.sort_order, w.id
                """,
                (owner_account_id, include_disabled),
            )
            rows = [dict(row) for row in cursor.fetchall()]
    return [_public_row(row) for row in rows]


def add_item(url: str, values: dict, owner_account_id: int | None = None) -> dict:
    try:
        security = validate_security(values)
    except ValueError as exc:
        raise WatchlistError(422, str(exc)) from exc
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            _write_guard(cursor)
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"watchlist:{security['market']}:{security['stock_code']}",),
            )
            cursor.execute(
                "SELECT * FROM securities WHERE market = %s AND stock_code = %s FOR UPDATE",
                (security["market"], security["stock_code"]),
            )
            existing = cursor.fetchone()
            if existing is None:
                try:
                    cursor.execute(
                        """
                        INSERT INTO securities (
                            market, stock_code, stock_name, currency, quote_source,
                            quote_symbol, exchange_suffix, cik, industry
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            security["market"], security["stock_code"],
                            str(security["stock_name"]).strip(), security["currency"],
                            security["quote_source"], str(security["quote_symbol"]).strip(),
                            security["exchange_suffix"], security["cik"],
                            security.get("industry"),
                        ),
                    )
                except errors.UniqueViolation as exc:
                    raise WatchlistError(409, "行情代码已被其他证券使用，请核对") from exc
                security_id = cursor.fetchone()["id"]
            else:
                security_id = existing["id"]
                if existing["quote_source"] != security["quote_source"] or existing["quote_symbol"] != str(security["quote_symbol"]).strip():
                    raise WatchlistError(409, "该证券已登记不同的行情源，请核对后重试")
            cursor.execute(
                """
                SELECT id, enabled FROM watchlist_items
                WHERE security_id = %s AND owner_account_id IS NOT DISTINCT FROM %s
                FOR UPDATE
                """,
                (security_id, owner_account_id),
            )
            item = cursor.fetchone()
            if item is not None and item["enabled"]:
                raise WatchlistError(409, "该证券已在关注列表中")
            details = (
                values["group_name"], values["note"], values["research_url"], values["sort_order"],
            )
            if item is None:
                cursor.execute(
                    """
                    INSERT INTO watchlist_items (
                        security_id, owner_account_id, group_name, note,
                        research_url, sort_order, enabled
                    ) VALUES (%s, %s, %s, %s, %s, %s, true) RETURNING id
                    """,
                    (security_id, owner_account_id, *details),
                )
                item_id = cursor.fetchone()["id"]
                action = "created"
            else:
                item_id = item["id"]
                cursor.execute(
                    """
                    UPDATE watchlist_items SET group_name = %s, note = %s,
                        research_url = %s, sort_order = %s, enabled = true, updated_at = now()
                    WHERE id = %s
                    """,
                    (*details, item_id),
                )
                action = "restored"
            _queue_quote_collection(cursor, security_id)
    return {"id": item_id, "security_id": security_id, "action": action}


def update_item(
    url: str,
    item_id: int,
    values: dict,
    owner_account_id: int | None = None,
) -> dict:
    with connect(url) as conn:
        with conn.cursor() as cursor:
            _write_guard(cursor)
            cursor.execute(
                """
                UPDATE watchlist_items SET group_name = %s, note = %s,
                    research_url = %s, sort_order = %s, updated_at = now()
                WHERE id = %s AND enabled = true
                  AND owner_account_id IS NOT DISTINCT FROM %s RETURNING id
                """,
                (
                    values["group_name"], values["note"], values["research_url"],
                    values["sort_order"], item_id, owner_account_id,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise WatchlistError(404, "关注项不存在或已停用")
    return {"id": row[0]}


def set_item_enabled(
    url: str,
    item_id: int,
    enabled: bool,
    owner_account_id: int | None = None,
) -> dict:
    with connect(url) as conn:
        with conn.cursor() as cursor:
            _write_guard(cursor)
            cursor.execute(
                """
                UPDATE watchlist_items SET enabled = %s, updated_at = now()
                WHERE id = %s AND enabled <> %s
                  AND owner_account_id IS NOT DISTINCT FROM %s
                RETURNING security_id
                """,
                (enabled, item_id, enabled, owner_account_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise WatchlistError(404, "关注项不存在或已处于目标状态")
            if enabled:
                _queue_quote_collection(cursor, row[0])
    return {"id": item_id, "enabled": enabled}


def quote_batch(
    url: str,
    market: str,
    codes: list[str],
    now: datetime | None = None,
    owner_account_id: int | None = None,
) -> dict:
    if market not in MARKETS:
        raise WatchlistError(422, "不支持的市场")
    canonical = {code: canonical_code(market, code) for code in codes}
    current = now or datetime.now(timezone.utc)
    market_date = current.astimezone(ZoneInfo(TIMEZONES[market])).date()
    quotes = []
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            for requested in codes:
                code = canonical[requested]
                cursor.execute(
                    """
                    SELECT q.trade_date, q.close FROM daily_quotes q
                    JOIN securities s ON s.market = q.market AND s.stock_code = q.stock_code
                    JOIN watchlist_items w ON w.security_id = s.id AND w.enabled = true
                      AND w.owner_account_id IS NOT DISTINCT FROM %s
                    WHERE q.market = %s AND q.stock_code = %s
                    ORDER BY q.trade_date DESC LIMIT 20
                    """,
                    (owner_account_id, market, code),
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
