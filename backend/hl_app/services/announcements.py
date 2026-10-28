"""关注股官方公告看板：批量摘要与详情分页（只读，只查库，不触发外网）。

合同：docs/WATCHLIST_ANNOUNCEMENTS_UI_PLAN.md（v2，2026-10-21 审核通过）。

- 逻辑公告按 (source_id, source_record_id) 计一条；展示最高 revision，
  首次发现时间沿用 revision=1（关联表锚定首版，查询经关联表定位后取
  最高版本）。SEC /A 修订是独立 source_record_id，仍为独立披露。
- 窗口：北京时间自然日 [今日-(N-1), 今日]。精确时间转北京时间判定；
  仅日期记录用来源原始日期比较（不补午夜，承认至多 1 天时区归属误差）；
  时间未知（precision='unknown'）不计数、不进 latest 候选（除非全部
  未知），详情按发现时间排序并标注。
- 降级限定：仅 undefined_table（公告 schema 未安装）时 initialized=false、
  count=None（前端显示「—」）；其他数据库错误照常抛服务异常。
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.errors
from psycopg2.extras import RealDictCursor

from ..config import database_url

logger = logging.getLogger(__name__)

BJ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc

DETAIL_MAX_DAYS = 90
DETAIL_MAX_LIMIT = 50
SUMMARY_MAX_DAYS = 90


class AnnouncementConfigError(RuntimeError):
    """公告配置非法（如非法间隔值）；读接口按服务异常暴露，不静默。"""


def _read_rows(sql, params=()):
    with psycopg2.connect(database_url("runtime"), cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def _announcements_enabled() -> bool:
    value = os.environ.get("HL_APP_COLLECTORS_ENABLED", "false").lower()
    if value not in {"true", "false"}:
        raise AnnouncementConfigError("HL_APP_COLLECTORS_ENABLED 必须是 true 或 false")
    return value == "true"


def _interval_minutes() -> int:
    try:
        seconds = int(os.environ.get("HL_APP_COLLECTION_INTERVAL_SECONDS", "3600"))
    except ValueError as exc:
        raise AnnouncementConfigError("HL_APP_COLLECTION_INTERVAL_SECONDS 必须是整数") from exc
    if seconds < 60:
        raise AnnouncementConfigError("HL_APP_COLLECTION_INTERVAL_SECONDS 不能小于 60")
    return max(1, seconds // 60)


def _window(days: int, now: datetime | None = None) -> tuple[date, date]:
    """北京时间自然日窗口 [start, today]（含今天）。"""
    now = now or datetime.now(UTC)
    today_bj = now.astimezone(BJ).date()
    return today_bj - timedelta(days=days - 1), today_bj


def _in_window(published_at, precision: str, published_raw: str,
               start: date, end: date) -> bool:
    """§2 窗口口径。unknown 一律不计入。"""
    if published_at is not None:
        d = published_at.astimezone(BJ).date()
        return start <= d <= end
    if precision == "day" and published_raw:
        try:
            d = date.fromisoformat(published_raw[:10])
        except ValueError:
            return False
        return start <= d <= end
    return False


def _iso(value):
    return value.isoformat() if value is not None else None


# ── 逻辑公告装载 ───────────────────────────────────────────

_LOGICAL_SQL = """
    SELECT sec.stock_code, sec.relation_reason AS association_reason,
           a.id, a.source_id, a.source_record_id, a.revision,
           a.title, a.form_type, a.original_url, a.published_at,
           a.published_at_precision, a.source_published_raw, a.source_timezone,
           a.first_observed_at, a.is_backfill, a.content_nature,
           a.relation_reason, a.media_source, a.excerpt, a.title_zh,
           a.summary_zh, a.importance, s.source_kind, s.platform
    FROM watchlist_announcement_security sec
    JOIN watchlist_announcement anchor ON anchor.id = sec.announcement_id
    JOIN watchlist_announcement a
      ON a.source_id = anchor.source_id
     AND a.source_record_id = anchor.source_record_id
    JOIN watchlist_announcement_source s ON s.source_id = a.source_id
    WHERE sec.market = 'US'
"""


def _load_logical_announcements() -> dict[str, list[dict]]:
    """按 stock_code 聚合逻辑公告：{code: [{(source_id, source_record_id)
    一条，display=最高 revision 行, first_observed=revision 1 行}]，已排序}。

    排序固定且稳定：发布时间倒序（NULL 在后）→ 首次发现倒序 → id 倒序。
    """
    rows = _read_rows(_LOGICAL_SQL)
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        grouped.setdefault(
            (row["stock_code"], row["source_id"], row["source_record_id"]),
            []).append(row)
    by_code: dict[str, list[dict]] = {}
    for (code, _sid, _rid), versions in grouped.items():
        versions.sort(key=lambda r: r["revision"])
        display = versions[-1]           # 最高 revision 展示
        first = versions[0]              # revision=1 的首次发现时间
        by_code.setdefault(code, []).append({
            **display,
            "first_observed_at": first["first_observed_at"],
            "revision_count": len(versions),
        })
    for code in by_code:
        # 排序固定且稳定：发布时间倒序（NULL 在后）→ 首次发现倒序 → id 倒序
        def sort_key(r):
            pub = r["published_at"]
            return (
                0 if pub is not None else 1,
                -pub.timestamp() if pub else 0,
                -r["first_observed_at"].timestamp(),
                -r["id"],
            )
        by_code[code].sort(key=sort_key)
    return by_code


def _public_item(row: dict) -> dict:
    precision = row["published_at_precision"]
    time_unknown = precision == "unknown"
    return {
        "title": row["title"],
        "form_type": row["form_type"],
        "url": row["original_url"],
        "source_kind": row["source_kind"],
        "published_at": _iso(row["published_at"]),
        "published_precision": precision,
        "published_raw": row["source_published_raw"],
        "source_timezone": row["source_timezone"],
        "time_unknown": time_unknown,
        "date_only": precision == "day",
        "first_observed_at": _iso(row["first_observed_at"]),
        "revision": row["revision"],
        "is_backfill": row["is_backfill"],
        "stock_code": row.get("stock_code"),
        "content_nature": row.get("content_nature"),
        "relation_reason": row.get("relation_reason"),
        "media_source": row.get("media_source"),
        "excerpt": row.get("excerpt"),
        "platform": row.get("platform"),
        "title_zh": row.get("title_zh"),
        "summary_zh": row.get("summary_zh"),
        "importance": row.get("importance"),
    }


def _latest_for(rows: list[dict]) -> dict | None:
    """最新一条（不限窗口）：先比较日期新旧——仅日期记录按来源原始日期
    参与比较，精度不作为新旧优先级（同日精确时间优先于仅日期）；
    时间未知记录只在全部记录时间未知时才作候选（取最近发现并标注）。"""
    if not rows:
        return None

    def sort_key(row):
        # 精确时间先转北京时间再取日期（窗口口径=北京时间自然日），
        # 与仅日期的来源原始日期同口径比较；时间比较用时间戳，
        # 不受数据库会话时区/ISO 偏移字面量影响
        if row["published_at"] is not None:
            pub = row["published_at"]
            return (pub.astimezone(BJ).date().isoformat(), 1, pub.timestamp())
        if row["published_at_precision"] == "day" and row["source_published_raw"]:
            return (row["source_published_raw"][:10], 0, 0.0)
        return None

    timed = [r for r in rows if sort_key(r) is not None]
    if timed:
        best = max(timed, key=sort_key)
        item = _public_item(best)
        item["latest_by"] = "published"
        return item
    best = max(rows, key=lambda r: (r["first_observed_at"], r["id"]))
    item = _public_item(best)
    item["latest_by"] = "first_observed"
    item["time_unknown"] = True
    return item


# ── 来源状态（§3 五态）─────────────────────────────────────

def _source_entry(kind, *, state, message, row=None, check=None,
                  gaps=None, interval=None):
    return {
        "kind": kind,
        "state": state,
        "message": message,
        "source_status": (row or {}).get("source_status"),
        "platform": (row or {}).get("platform"),
        "reason": (row or {}).get("status_reason"),
        "last_attempt_at": _iso((check or {}).get("last_attempt_at")),
        "last_attempt_status": (check or {}).get("last_attempt_status"),
        "last_error_class": (check or {}).get("last_error_class"),
        "last_success_at": _iso((check or {}).get("last_success_at")),
        "next_due_at": _iso((check or {}).get("next_due_at")),
        "checked_through": _iso((check or {}).get("checked_through")),
        "effective_interval_minutes": (check or {}).get(
            "effective_interval_minutes") or interval,
        "open_gaps": len(gaps or []),
        "earliest_gap_start": _iso(min(g[0] for g in gaps) if gaps else None),
    }


def _state_from_check(status, error_class, gaps):
    if status in ("success_new", "success_empty") and not gaps:
        return "ok", "正常"
    if status == "failed":
        return "failed", f"来源失败：{error_class or '未知错误'}"
    return ("partial",
            "历史覆盖不足，存在未补齐缺口" if gaps else "覆盖不完整（partial）")


def _company_sources(source_rows, checks, gaps_by_source, cik, enabled):
    """组装一家公司的 SEC + IR 来源状态列表（§3 五态）。"""
    entries = []
    sec_row = next((r for r in source_rows
                    if r["source_kind"] == "sec" and r["issuer_identifier"] == cik),
                   None) if cik else None
    if not enabled:
        check = checks.get(sec_row["source_id"]) if sec_row else None
        gaps = gaps_by_source.get(sec_row["source_id"], []) if sec_row else []
        entries.append(_source_entry(
            "sec", state="disabled_global", message="公告采集未启用",
            row=sec_row, check=check, gaps=gaps))
    elif cik is None:
        entries.append(_source_entry(
            "sec", state="not_configured", message="缺 CIK，SEC 来源不可用"))
    elif sec_row is None:
        entries.append(_source_entry(
            "sec", state="not_configured",
            message="SEC 来源未注册（采集尚未运行或未启用）"))
    else:
        check = checks.get(sec_row["source_id"])
        gaps = gaps_by_source.get(sec_row["source_id"], [])
        interval = (sec_row["interval_minutes"]
                    or _interval_minutes())
        if check is None:
            entries.append(_source_entry(
                "sec", state="pending_first_check", message="待首次检查",
                row=sec_row, gaps=gaps, interval=interval))
        else:
            state, message = _state_from_check(
                check["last_attempt_status"], check["last_error_class"], gaps)
            entries.append(_source_entry(
                "sec", state=state, message=message, row=sec_row, check=check,
                gaps=gaps, interval=interval))

    ir_rows = [r for r in source_rows
               if r["source_kind"] == "ir" and r["issuer_identifier"] == cik]
    if not ir_rows:
        entries.append(_source_entry(
            "ir",
            state="not_configured" if enabled else "disabled_global",
            message="IR 来源未配置（登记官方 IR 入口后接入）" if enabled
                    else "公告采集未启用"))
    for row in ir_rows:
        check = checks.get(row["source_id"])
        gaps = gaps_by_source.get(row["source_id"], [])
        interval = (row["interval_minutes"]
                    or _interval_minutes())
        # §3：历史状态字段（last_attempt/last_success/next_due/checked_through/
        # 缺口摘要）有值就返回，与当前状态无关——blocked/待验证不抹掉历史
        if not enabled:
            entries.append(_source_entry(
                "ir", state="disabled_global", message="公告采集未启用",
                row=row, check=check, gaps=gaps, interval=interval))
        elif row["source_status"] == "blocked":
            entries.append(_source_entry(
                "ir", state="blocked",
                message=f"来源受阻：{row['status_reason'] or '未说明'}",
                row=row, check=check, gaps=gaps, interval=interval))
        elif row["source_status"] == "pending_verification":
            entries.append(_source_entry(
                "ir", state="pending_verification",
                message=f"待验证：{row['status_reason'] or '未说明'}",
                row=row, check=check, gaps=gaps, interval=interval))
        elif not row["enabled"]:
            entries.append(_source_entry(
                "ir", state="not_configured",
                message=row["status_reason"] or "来源未启用",
                row=row, check=check, gaps=gaps, interval=interval))
        elif check is None:
            entries.append(_source_entry(
                "ir", state="pending_first_check", message="待首次检查",
                row=row, gaps=gaps, interval=interval))
        else:
            state, message = _state_from_check(
                check["last_attempt_status"], check["last_error_class"], gaps)
            entries.append(_source_entry(
                "ir", state=state, message=message, row=row, check=check,
                gaps=gaps, interval=interval))
    return entries


# ── 批量摘要 ───────────────────────────────────────────────

def summary(days: int = 7) -> dict:
    """GET /watchlist/announcements 批量摘要（一次返回全部关注项）。"""
    days = max(1, min(int(days or 7), SUMMARY_MAX_DAYS))
    enabled = _announcements_enabled()
    start, end = _window(days)
    base = {
        "announcements_enabled": enabled,
        "days": days,
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "timezone": "Asia/Shanghai"},
    }
    try:
        items = _read_rows(
            "SELECT w.id, s.market, s.stock_code, s.stock_name "
            "FROM watchlist_items w JOIN securities s ON s.id = w.security_id "
            "WHERE w.enabled = true ORDER BY w.sort_order, w.id")
    except psycopg2.errors.UndefinedTable:
        raise
    us_codes = [i["stock_code"] for i in items if i["market"] == "US"]
    cik_rows = _read_rows(
        "SELECT stock_code, cik FROM securities "
        "WHERE market='US' AND stock_code = ANY(%s)", (us_codes,)
    ) if us_codes else []
    try:
        source_rows = _read_rows(
            "SELECT source_id, source_kind, platform, issuer_identifier, "
            "       interval_minutes, enabled, source_status, status_reason "
            "FROM watchlist_announcement_source WHERE market='US'")
        checks = _read_rows(
            "SELECT source_id, last_attempt_at, last_attempt_status, "
            "       last_error_class, last_success_at, next_due_at, "
            "       checked_through, effective_interval_minutes "
            "FROM watchlist_announcement_check_state WHERE market='US'")
        gap_rows = _read_rows(
            "SELECT source_id, lower(gap_range) AS gap_start "
            "FROM watchlist_announcement_gap "
            "WHERE market='US' AND resolved_at IS NULL")
        logical = _load_logical_announcements()
    except psycopg2.errors.UndefinedTable:
        logger.warning("公告 schema 未安装（watchlist_announcements.sql 未执行）")
        return {**base, "initialized": False, "message": "公告功能尚未初始化",
                "degraded": False, "items": [
                    {"item_id": i["id"], "market": i["market"],
                     "stock_code": i["stock_code"], "count": None,
                     "latest": None, "sources": []}
                    for i in items
                ]}

    cik_by_code = {r["stock_code"]: str(r["cik"]).zfill(10)
                   for r in cik_rows if r["cik"]}
    checks_by_source = {c["source_id"]: c for c in checks}
    gaps_by_source: dict[int, list] = {}
    for g in gap_rows:
        gaps_by_source.setdefault(g["source_id"], []).append((g["gap_start"],))

    out_items = []
    degraded = False
    for item in items:
        code = item["stock_code"]
        if item["market"] != "US":
            out_items.append({
                "item_id": item["id"], "market": item["market"],
                "stock_code": code, "count": 0, "latest": None,
                "sources": [_source_entry(
                    "none", state="unsupported_market",
                    message="该市场未接入公告源")],
            })
            continue
        cik = cik_by_code.get(code)
        # 官方公告接口只含 SEC/IR；媒体条目属每日资讯层（/news/feed）
        rows = [r for r in logical.get(code, [])
                if r["source_kind"] in ("sec", "ir")]
        count = sum(1 for r in rows
                    if _in_window(r["published_at"], r["published_at_precision"],
                                  r["source_published_raw"] or "", start, end))
        sources = _company_sources(source_rows, checks_by_source,
                                   gaps_by_source, cik, enabled)
        if enabled and any(s["state"] in ("failed", "partial") for s in sources):
            degraded = True
        out_items.append({
            "item_id": item["id"], "market": "US", "stock_code": code,
            "count": count, "latest": _latest_for(rows), "sources": sources,
        })
    return {**base, "initialized": True, "degraded": degraded, "items": out_items}


# ── 详情分页 ───────────────────────────────────────────────

def detail(item_id: int, days: int = 90, offset: int = 0, limit: int = 20,
           source: str = "all") -> dict:
    """GET /watchlist/{item_id}/announcements 详情分页。

    先解析关注项，再按 market+stock_code 查询历史——删除后重新关注
    仍能看到原有记录。days 上限 90、limit 上限 50（超限取上限）。
    """
    days = max(1, min(int(days or DETAIL_MAX_DAYS), DETAIL_MAX_DAYS))
    limit = max(1, min(int(limit or 20), DETAIL_MAX_LIMIT))
    offset = max(0, int(offset or 0))
    if source not in ("all", "sec", "ir"):
        raise ValueError(f"非法来源筛选: {source!r}（允许 all|sec|ir）")
    enabled = _announcements_enabled()
    start, end = _window(days)

    items = _read_rows(
        "SELECT w.id, s.market, s.stock_code, s.stock_name "
        "FROM watchlist_items w JOIN securities s ON s.id = w.security_id "
        "WHERE w.id = %s AND w.enabled = true", (item_id,))
    if not items:
        raise LookupError("关注项不存在")
    item = items[0]
    base = {
        "item_id": item["id"], "market": item["market"],
        "stock_code": item["stock_code"], "stock_name": item["stock_name"],
        "announcements_enabled": enabled,
        "days": days, "offset": offset, "limit": limit, "source": source,
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "timezone": "Asia/Shanghai"},
    }
    if item["market"] != "US":
        return {**base, "supported": False, "initialized": True,
                "message": "该市场未接入公告源",
                "total": 0, "has_more": False, "items": [], "sources": []}
    cik_rows = _read_rows(
        "SELECT cik FROM securities WHERE market='US' AND stock_code=%s",
        (item["stock_code"],))
    cik = str(cik_rows[0]["cik"]).zfill(10) \
        if cik_rows and cik_rows[0]["cik"] else None
    try:
        logical = [r for r in _load_logical_announcements().get(
            item["stock_code"], []) if r["source_kind"] in ("sec", "ir")]
        source_rows = _read_rows(
            "SELECT source_id, source_kind, platform, issuer_identifier, "
            "       interval_minutes, enabled, source_status, status_reason "
            "FROM watchlist_announcement_source WHERE market='US'")
        checks = _read_rows(
            "SELECT source_id, last_attempt_at, last_attempt_status, "
            "       last_error_class, last_success_at, next_due_at, "
            "       checked_through, effective_interval_minutes "
            "FROM watchlist_announcement_check_state WHERE market='US'")
        gap_rows = _read_rows(
            "SELECT source_id, lower(gap_range) AS gap_start "
            "FROM watchlist_announcement_gap "
            "WHERE market='US' AND resolved_at IS NULL")
    except psycopg2.errors.UndefinedTable:
        return {**base, "supported": True, "initialized": False,
                "message": "公告功能尚未初始化",
                "total": 0, "has_more": False, "items": [], "sources": []}

    checks_by_source = {c["source_id"]: c for c in checks}
    gaps_by_source: dict[int, list] = {}
    for g in gap_rows:
        gaps_by_source.setdefault(g["source_id"], []).append((g["gap_start"],))

    def keep(row):
        if source != "all" and row["source_kind"] != source:
            return False
        if _in_window(row["published_at"], row["published_at_precision"],
                      row["source_published_raw"] or "", start, end):
            return True
        # 时间未知的记录无法按发布时间窗口判定：按发现时间在窗口内纳入，
        # 标注「时间未知」（§2：不把发现时间当成近期发布）
        if row["published_at_precision"] == "unknown":
            return start <= row["first_observed_at"].astimezone(BJ).date() <= end
        return False

    filtered = [r for r in logical if keep(r)]
    total = len(filtered)
    page = filtered[offset:offset + limit]
    return {**base, "supported": True, "initialized": True,
            "total": total, "has_more": offset + limit < total,
            "items": [_public_item(r) for r in page],
            "sources": _company_sources(source_rows, checks_by_source,
                                        gaps_by_source, cik, enabled)}
