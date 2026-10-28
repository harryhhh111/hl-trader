"""每日资讯信息流：feed 与置顶（只读，只查库，不触发外网）。

合同：docs/WATCHLIST_DAILY_NEWS_MVP_PLAN.md（2026-10-22 审核通过）§4。

- 时间口径：北京时间自然日；每条标注原始时区与北京时间。
- 折叠去重（展示层，不动存储）：规范化标题（小写、去标点、去来源后缀
  " - Reuters"）相同且发布时间相差 ≤48h 的媒体报道折成一组；标题不同
  不折叠——后续更新、修订和不同观点不得被误删。时间未知的媒体条目
  不参与折叠（无法比较发布时间）。
- 缺失时间条目单独标注，按首次发现时间排序。
- 降级限定同公告片：仅公告 schema 缺失（undefined_table）降级，
  其他数据库错误照常 503。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

import psycopg2.errors

from . import announcements as ann
from .announcements import BJ, UTC

logger = logging.getLogger(__name__)

FOLD_WINDOW = timedelta(hours=48)
OFFICIAL_KINDS = ("sec", "ir")


# ── 折叠（展示层）──────────────────────────────────────────

# 已知出版商后缀名单（仅这些后缀才可从标题剥离；横线后的语义子句
# 如 " - raised"/" - cut" 不是媒体名，绝不能剥离后误折叠含义相反的新闻）
KNOWN_PUBLISHERS = {
    "reuters", "yahoo finance", "cnbc", "bloomberg.com", "bloomberg",
    "barron's", "seeking alpha", "benzinga", "the motley fool",
    "marketwatch", "investing.com", "associated press", "ap", "barchart",
    "gurufocus", "zacks", "fortune", "business insider", "tom's hardware",
    "blockspace", "24/7 wall st", "south china morning post", "wsj",
    "financial times", "nikkei asia", "globenewswire",
}


def normalize_title(title: str, media_source: str = "") -> str:
    """规范化标题：剥离确认匹配的媒体名后缀（等于该条 media_source 或
    已知出版商名单）、小写、去标点、压缩空白。
    横线后其他内容一律保留——它是标题语义的一部分。"""
    t = title.strip()
    m = re.search(r"\s+-\s+([^-]{1,60})$", t)
    if m:
        suffix = m.group(1).strip().lower()
        if suffix == (media_source or "").strip().lower() \
                or suffix in KNOWN_PUBLISHERS:
            t = t[:m.start()]
    t = t.lower()
    t = re.sub(r"[^\w\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def fold_media(rows: list[dict]) -> list[list[dict]]:
    """媒体报道折叠分组。返回 list of clusters（每 cluster 为一组同题条目）。

    规则：规范化标题相同 + 发布时间相差 ≤48h（以组内首条为锚）。
    时间未知条目不参与折叠（各自成组）。
    """
    by_title: dict[str, list[dict]] = {}
    for row in rows:
        by_title.setdefault(
            normalize_title(row["title"], row.get("media_source") or ""),
            []).append(row)
    clusters: list[list[dict]] = []
    for group in by_title.values():
        timed = sorted((r for r in group if r["published_at"] is not None),
                       key=lambda r: r["published_at"])
        untimed = [r for r in group if r["published_at"] is None]
        anchor = None
        current: list[dict] = []
        for row in timed:
            if anchor is None or row["published_at"] - anchor > FOLD_WINDOW:
                if current:
                    clusters.append(current)
                current = [row]
                anchor = row["published_at"]
            else:
                current.append(row)
        if current:
            clusters.append(current)
        clusters.extend([r] for r in untimed)  # 未知时间各自成组
    return clusters


# ── 条目成形 ───────────────────────────────────────────────

def _reason_from_row(row: dict) -> dict | None:
    text = (row.get("association_reason") or row.get("relation_reason") or "").strip()
    if not text:
        return None
    return {
        "text": text,
        "source_id": row["source_id"],
        "source_record_id": row["source_record_id"],
        "url": row["original_url"],
    }


def _merge_by_announcement(rows) -> list[dict]:
    """合并逻辑公告及其公司关联；共享记录的公司级理由无法归属时留空。"""
    grouped: dict[tuple, list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["source_id"], row["source_record_id"]), []).append(row)
    merged = []
    for versions in grouped.values():
        codes = sorted({row["stock_code"] for row in versions})
        relations = {code: [] for code in codes}
        for row in versions:
            if len(codes) == 1 or row.get("association_reason"):
                reason = _reason_from_row(row)
                if reason:
                    relations[row["stock_code"]].append(reason)
        merged.append({**versions[0], "stock_codes": codes,
                       "_relations": relations})
    return merged


def _combined_relations(rows: list[dict], stock_codes: list[str]) -> dict:
    combined = {code: [] for code in stock_codes}
    seen = {code: set() for code in stock_codes}
    for row in rows:
        for code, reasons in row.get("_relations", {}).items():
            if code not in combined:
                continue
            for reason in reasons:
                key = (reason["text"], reason["source_id"],
                       reason["source_record_id"], reason["url"])
                if key not in seen[code]:
                    seen[code].add(key)
                    combined[code].append(reason)
    for reasons in combined.values():
        reasons.sort(key=lambda r: (r["text"], r["source_id"],
                                    r["source_record_id"], r["url"]))
    return combined


def _legacy_relation(stock_codes: list[str], relations: dict) -> str | None:
    if len(stock_codes) != 1:
        return None
    texts = {reason["text"] for reason in relations[stock_codes[0]]}
    return next(iter(texts)) if len(texts) == 1 else None


def _apply_relations(entry: dict, rows: list[dict]) -> dict:
    stock_codes = sorted({code for row in rows
                          for code in row.get("stock_codes", [row["stock_code"]])})
    relations = _combined_relations(rows, stock_codes)
    entry["stock_code"] = stock_codes[0]
    entry["stock_codes"] = stock_codes
    entry["stock_relations"] = [
        {"stock_code": code, "reasons": relations[code]} for code in stock_codes]
    entry["relation_reason"] = _legacy_relation(stock_codes, relations)
    return entry


def _entry_base(row: dict) -> dict:
    day = _bj_day(row)
    entry = {
        "title": row["title"],
        "url": row["original_url"],
        "source_kind": row["source_kind"],
        "platform": row.get("platform"),
        "media_source": row.get("media_source"),
        "form_type": row["form_type"],
        "content_nature": row.get("content_nature"),
        "excerpt": row.get("excerpt"),
        "published_at": ann._iso(row["published_at"]),
        "published_precision": row["published_at_precision"],
        "published_raw": row["source_published_raw"],
        "source_timezone": row["source_timezone"],
        "time_unknown": row["published_at_precision"] == "unknown",
        "date_only": row["published_at_precision"] == "day",
        "first_observed_at": ann._iso(row["first_observed_at"]),
        "is_backfill": row["is_backfill"],
        "bj_day": day.isoformat() if day else None,
        "title_zh": row.get("title_zh"),
        "summary_zh": row.get("summary_zh"),
        "importance": row.get("importance"),
    }
    return _apply_relations(entry, [row])


def _media_group_entry(cluster: list[dict]) -> dict:
    """折叠组保留代表字段、全部公司及其有明确归属的理由出处。"""
    rep = max(cluster, key=lambda r: (r["published_at"] is not None,
                                      r["published_at"] or datetime.min
                                      .replace(tzinfo=UTC),
                                      r["first_observed_at"], r["id"]))
    entry = _apply_relations(_entry_base(rep), cluster)
    publishers = sorted({(r.get("media_source") or "unknown")
                         for r in cluster})
    entry["fold_count"] = len(publishers)
    seen_urls: set[str] = set()
    fold_sources = []
    for r in sorted(cluster, key=lambda r: (
            r["published_at"] or datetime.max.replace(tzinfo=UTC))):
        key = (r.get("media_source") or "", r["original_url"])
        if key in seen_urls:
            continue
        seen_urls.add(key)
        fold_sources.append({
            "name": r.get("media_source") or "", "url": r["original_url"],
            "published_at": ann._iso(r["published_at"]),
            "platform": r.get("platform")})
    entry["fold_sources"] = fold_sources
    entry["publishers"] = publishers
    return entry


def _sort_entries(entries: list[dict]) -> list[dict]:
    """发布时间倒序（NULL 在后）→ 首次发现倒序。"""
    def key(e):
        pub = e["published_at"]
        fo = e["first_observed_at"]
        return (0 if pub else 1,
                -datetime.fromisoformat(pub).timestamp() if pub else 0,
                -datetime.fromisoformat(fo).timestamp() if fo else 0)
    return sorted(entries, key=key)


# ── 窗口判定（feed 单日 / pinned 24h）──────────────────────

def _bj_day(row: dict) -> date | None:
    """条目的北京时间日期（展示分组/窗口口径统一用）。"""
    if row["published_at"] is not None:
        return row["published_at"].astimezone(BJ).date()
    if row["published_at_precision"] == "day" and row["source_published_raw"]:
        try:
            return date.fromisoformat(row["source_published_raw"][:10])
        except ValueError:
            return None
    return row["first_observed_at"].astimezone(BJ).date()


# ── 媒体来源状态（并入既有五态语义）─────────────────────────

def _news_source_entries(source_rows, checks, gaps_by_source, enabled,
                         kinds=("sec", "ir", "media")):
    """全部来源状态（官方 SEC/IR + 媒体）——官方失败状态也返回，
    区分「没有公告」与「采集失败」。"""
    entries = []
    for row in source_rows:
        kind = row["source_kind"]
        if kind not in kinds:
            continue
        check = checks.get(row["source_id"])
        gaps = gaps_by_source.get(row["source_id"], [])
        interval = row["interval_minutes"]
        if not enabled:
            entries.append(ann._source_entry(
                kind, state="disabled_global", message="公告采集未启用",
                row=row, check=check, gaps=gaps, interval=interval))
        elif row["source_status"] == "blocked":
            entries.append(ann._source_entry(
                kind, state="blocked",
                message=f"来源受阻：{row['status_reason'] or '未说明'}",
                row=row, check=check, gaps=gaps, interval=interval))
        elif row["source_status"] == "pending_verification":
            entries.append(ann._source_entry(
                kind, state="pending_verification",
                message=f"待验证：{row['status_reason'] or '未说明'}",
                row=row, check=check, gaps=gaps, interval=interval))
        elif not row["enabled"]:
            entries.append(ann._source_entry(
                kind, state="not_configured",
                message=row["status_reason"] or "来源未启用",
                row=row, check=check, gaps=gaps, interval=interval))
        elif check is None:
            entries.append(ann._source_entry(
                kind, state="pending_first_check", message="待首次检查",
                row=row, gaps=gaps, interval=interval))
        else:
            state, message = ann._state_from_check(
                check["last_attempt_status"], check["last_error_class"], gaps)
            entries.append(ann._source_entry(
                kind, state=state, message=message, row=row, check=check,
                gaps=gaps, interval=interval))
    return entries


def _load_sources_block():
    """全部来源状态（官方 + 媒体），供 feed/pinned 的状态区。"""
    source_rows = ann._read_rows(
        "SELECT source_id, source_kind, platform, issuer_identifier, "
        "       interval_minutes, enabled, source_status, status_reason "
        "FROM watchlist_announcement_source WHERE market='US'")
    checks = ann._read_rows(
        "SELECT source_id, last_attempt_at, last_attempt_status, "
        "       last_error_class, last_success_at, next_due_at, "
        "       checked_through, effective_interval_minutes "
        "FROM watchlist_announcement_check_state WHERE market='US'")
    gap_rows = ann._read_rows(
        "SELECT source_id, lower(gap_range) AS gap_start "
        "FROM watchlist_announcement_gap "
        "WHERE market='US' AND resolved_at IS NULL")
    checks_by_source = {c["source_id"]: c for c in checks}
    gaps_by_source: dict[int, list] = {}
    for g in gap_rows:
        gaps_by_source.setdefault(g["source_id"], []).append((g["gap_start"],))
    return source_rows, checks_by_source, gaps_by_source


# ── feed / pinned ──────────────────────────────────────────

FEED_DEFAULT_DAYS = 7  # date 缺省时返回最近 7 个北京时间自然日（时间流分组）


def feed(date_str: str | None = None, stock: str = "", kind: str = "all") -> dict:
    """GET /watchlist/news/feed：北京时间自然日信息流（折叠后返回）。

    date 缺省返回最近 7 个自然日（前端按日分组为时间流）；date 指定时
    只返回该日。未知时间条目按首次发现时间入窗口并标注。
    """
    if kind not in ("all", "official", "media"):
        raise ValueError(f"非法类型筛选: {kind!r}（允许 all|official|media）")
    enabled = ann._announcements_enabled()
    today = datetime.now(UTC).astimezone(BJ).date()
    if date_str:
        try:
            days = [date.fromisoformat(date_str.strip())]
        except ValueError:
            raise ValueError(f"非法日期: {date_str!r}（需要 YYYY-MM-DD）")
    else:
        days = [today - timedelta(days=i) for i in range(FEED_DEFAULT_DAYS)]
    stock = stock.strip().upper()
    base = {"days": [d.isoformat() for d in days], "kind": kind,
            "stock": stock, "timezone": "Asia/Shanghai",
            "announcements_enabled": enabled}
    # watchlist_item 是基础表：缺失不属于公告降级范围，照常抛服务异常
    watched = {r["stock_code"] for r in ann._read_rows(
        "SELECT s.stock_code FROM watchlist_items w JOIN securities s ON s.id = w.security_id WHERE s.market='US' AND w.enabled=true")}
    try:
        logical = ann._load_logical_announcements()
        source_rows, checks, gaps_by_source = _load_sources_block()
    except psycopg2.errors.UndefinedTable:
        logger.warning("公告 schema 未安装（watchlist_announcements.sql 未执行）")
        return {**base, "initialized": False, "message": "公告功能尚未初始化",
                "entries": [], "sources": []}

    day_set = set(days)
    rows = _merge_by_announcement(
        r for code, rs in logical.items() for r in rs if code in watched)
    rows = [r for r in rows if _bj_day(r) in day_set]
    if kind == "official":
        rows = [r for r in rows if r["source_kind"] in OFFICIAL_KINDS]
    elif kind == "media":
        rows = [r for r in rows if r["source_kind"] == "media"]

    official_entries = []
    for r in rows:
        if r["source_kind"] in OFFICIAL_KINDS:
            e = _entry_base(r)
            e["fold_count"] = 1
            e["fold_sources"] = []
            official_entries.append(e)
    media_clusters = fold_media([r for r in rows if r["source_kind"] == "media"])
    entries = official_entries + [_media_group_entry(c) for c in media_clusters]
    if stock:
        entries = [entry for entry in entries if stock in entry["stock_codes"]]
    entries = _sort_entries(entries)

    sources = _news_source_entries(source_rows, checks, gaps_by_source, enabled)
    return {**base, "initialized": True, "entries": entries, "sources": sources}


PINNED_WINDOW = timedelta(hours=48)
PINNED_MAX = 10


def _within_48h_pinned(row: dict, now: datetime) -> bool:
    """置顶候选（近 48h，发布时间口径；不回退发现时间）：
    秒/分精度按 published_at；仅日期按北京日期为今天或昨天；
    unknown 永不进。"""
    if row["published_at"] is not None:
        return row["published_at"] >= now - PINNED_WINDOW
    if row["published_at_precision"] == "day" and row["source_published_raw"]:
        try:
            d = date.fromisoformat(row["source_published_raw"][:10])
        except ValueError:
            return False
        today_bj = now.astimezone(BJ).date()
        return d in (today_bj, today_bj - timedelta(days=1))
    return False


def _ai_available(rows: list[dict], now: datetime) -> bool:
    """AI 持续不可用判定：近 24h 有新入库条目且全部无 importance
    → 增强持续失败，回退机械规则。无新条目时无法判定失败，用正常规则。"""
    recent = [r for r in rows
              if r["first_observed_at"] is not None
              and r["first_observed_at"] >= now - timedelta(hours=24)]
    if not recent:
        return True
    return any(r.get("importance") for r in recent)


def pinned() -> dict:
    """GET /watchlist/news/pinned（v1.1：热度 × 重要性，封顶 10）。

    候选：近 48h（发布时间口径，仅日期/unknown 不回退发现时间）。
    入选：importance='high' 或折叠后来源数 ≥3；官方媒体一视同仁。
    排序：high 优先 → 来源数 → 发布时间。封顶 10。
    AI 持续不可用（近 24h 新条目全部无 importance）时回退机械规则：
    ≥3 源媒体组 + 近 24h 官方披露，同样封顶 10。
    """
    enabled = ann._announcements_enabled()
    now = datetime.now(UTC)
    base = {"as_of": now.isoformat(), "timezone": "Asia/Shanghai",
            "announcements_enabled": enabled}
    try:
        watched = {r["stock_code"] for r in ann._read_rows(
            "SELECT s.stock_code FROM watchlist_items w JOIN securities s ON s.id = w.security_id WHERE s.market='US' AND w.enabled=true")}
    except psycopg2.errors.UndefinedTable:
        raise  # watchlist_item 是基础表：缺失不降级，照常 503
    try:
        logical = ann._load_logical_announcements()
    except psycopg2.errors.UndefinedTable:
        return {**base, "initialized": False, "message": "公告功能尚未初始化",
                "entries": [], "ai_available": True}
    rows = _merge_by_announcement(
        r for code, rs in logical.items() for r in rs if code in watched)
    ai_ok = _ai_available(rows, now)

    candidates = [r for r in rows if _within_48h_pinned(r, now)]
    official = []
    for r in candidates:
        if r["source_kind"] in OFFICIAL_KINDS:
            e = _entry_base(r)
            e["fold_count"] = 1
            e["fold_sources"] = []
            official.append(e)
    groups = [_media_group_entry(c)
              for c in fold_media([r for r in candidates
                                   if r["source_kind"] == "media"])]
    entries = official + groups
    if ai_ok:
        picked = [e for e in entries
                  if e.get("importance") == "high" or e["fold_count"] >= 3]
    else:
        # 机械回退：≥3 源媒体组 + 近 24h 官方披露
        picked = [e for e in groups if e["fold_count"] >= 3]
        picked += [e for e in official
                   if e["published_at"] and
                   datetime.fromisoformat(e["published_at"]) >= now - timedelta(hours=24)]
    picked.sort(key=lambda e: (
        0 if e.get("importance") == "high" else 1,
        -e["fold_count"],
        -(datetime.fromisoformat(e["published_at"]).timestamp()
          if e["published_at"] else 0),
    ))
    return {**base, "initialized": True, "ai_available": ai_ok,
            "entries": picked[:PINNED_MAX]}
