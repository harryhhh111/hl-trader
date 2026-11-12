from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

from psycopg2.extras import RealDictCursor

from ..db import connect
from ..migration.common import BundleError
from ..migration.service import _lock, _mode, _require_drained, business_write_guard


class SyncStoreError(BundleError):
    pass


def _hash_event(event) -> str:
    payload = event.model_dump(mode="json")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _scalar(row) -> object:
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def _state(cursor) -> dict:
    cursor.execute("SELECT * FROM hl_news_sync_state WHERE singleton = true")
    row = cursor.fetchone()
    if row is None:
        raise SyncStoreError("资讯同步状态未初始化")
    return dict(row)


def _business_hash(cursor) -> str:
    cursor.execute(
        "SELECT * FROM watchlist_announcement_source WHERE ingest_mode = 'stock_data' ORDER BY source_id"
    )
    sources = [dict(row) for row in cursor.fetchall()]
    ids = [row["source_id"] for row in sources]
    announcements = []
    relations = []
    checks = []
    projections = []
    if ids:
        cursor.execute("SELECT * FROM watchlist_announcement WHERE source_id = ANY(%s) ORDER BY id", (ids,))
        announcements = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT r.* FROM watchlist_announcement_security r JOIN watchlist_announcement a ON a.id = r.announcement_id WHERE a.source_id = ANY(%s) ORDER BY r.announcement_id, r.market, r.stock_code",
            (ids,),
        )
        relations = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT * FROM watchlist_announcement_check_state WHERE source_id = ANY(%s) ORDER BY source_id", (ids,))
        checks = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT * FROM hl_news_sync_source_state WHERE local_source_id = ANY(%s) ORDER BY local_source_id", (ids,))
        projections = [dict(row) for row in cursor.fetchall()]
    payload = {
        "sources": sources, "announcements": announcements, "relations": relations,
        "checks": checks, "projections": projections,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_bootstrap_scope_empty(cursor) -> None:
    cursor.execute("SELECT EXISTS (SELECT 1 FROM watchlist_announcement_source WHERE ingest_mode = 'stock_data')")
    if _scalar(cursor.fetchone()):
        raise SyncStoreError("bootstrap 要求上游同步来源范围为空")
    cursor.execute("SELECT EXISTS (SELECT 1 FROM hl_news_sync_event_receipt)")
    if _scalar(cursor.fetchone()):
        raise SyncStoreError("bootstrap 要求事件收据为空")


def _check_page_allowed(cursor, bootstrap: bool) -> dict:
    state = _state(cursor)
    if bootstrap:
        if _mode(cursor) != "maintenance":
            raise SyncStoreError("bootstrap 要求应用处于 maintenance")
        if state["bootstrap_status"] == "not_started":
            if state["next_cursor"] is not None:
                raise SyncStoreError("初始同步存在 cursor，不能从空状态重放")
            _require_bootstrap_scope_empty(cursor)
        elif state["bootstrap_status"] != "in_progress":
            raise SyncStoreError("bootstrap 已完成，不能重复初始化")
    else:
        business_write_guard(cursor)
        if state["bootstrap_status"] != "completed":
            raise SyncStoreError("普通增量要求 bootstrap 已完成")
    return state


def _source_natural_key(event) -> dict:
    return {
        "market": event.market, "source_kind": event.source_kind,
        "issuer_identifier": event.issuer_identifier, "entry_url": event.entry_url,
    }


def _apply_source(cursor, source_instance: str, event) -> None:
    parts = urlsplit(event.entry_url)
    if (
        parts.scheme != "https" or not parts.hostname or parts.username is not None
        or parts.password is not None or event.allowed_domain.lower() != parts.hostname.lower()
    ):
        raise SyncStoreError("来源 entry_url 与 allowed_domain 不一致或不是 HTTPS")
    natural = _source_natural_key(event)
    cursor.execute(
        "SELECT * FROM hl_news_sync_source_map WHERE source_instance = %s AND upstream_source_id = %s",
        (source_instance, event.source_id),
    )
    mapping = cursor.fetchone()
    if mapping is None:
        cursor.execute(
            """
            SELECT * FROM watchlist_announcement_source
            WHERE market = %s AND source_kind = %s AND issuer_identifier = %s AND entry_url = %s
            """,
            tuple(natural.values()),
        )
        source = cursor.fetchone()
        if source is None:
            cursor.execute(
                """
                INSERT INTO watchlist_announcement_source (
                    market, source_kind, platform, issuer_identifier, allowed_domain,
                    entry_url, source_timezone, parser_version, interval_minutes, enabled,
                    source_status, status_reason, ingest_mode
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'stock_data')
                RETURNING *
                """,
                (
                    event.market, event.source_kind, event.platform, event.issuer_identifier,
                    event.allowed_domain, event.entry_url, event.source_timezone, event.parser_version,
                    event.interval_minutes, event.enabled, event.source_status, event.status_reason,
                ),
            )
            source = cursor.fetchone()
        elif source["ingest_mode"] != "stock_data":
            raise SyncStoreError("来源自然键已被本地直接采集占用")
        local_id = source["source_id"]
        cursor.execute(
            "INSERT INTO hl_news_sync_source_map (source_instance, upstream_source_id, local_source_id, natural_key) VALUES (%s, %s, %s, %s)",
            (source_instance, event.source_id, local_id, json.dumps(natural, sort_keys=True)),
        )
    else:
        local_id = mapping["local_source_id"]
        if mapping["natural_key"] != natural:
            raise SyncStoreError("上游来源自然键在映射后发生变化")
        cursor.execute("SELECT * FROM watchlist_announcement_source WHERE source_id = %s", (local_id,))
        source = cursor.fetchone()
        if source is None or source["ingest_mode"] != "stock_data":
            raise SyncStoreError("来源映射目标无效")
        if _source_natural_key(source) != natural:
            raise SyncStoreError("来源映射与自然键不一致")
        cursor.execute(
            """
            UPDATE watchlist_announcement_source SET
                platform = %s, allowed_domain = %s, source_timezone = %s, parser_version = %s,
                interval_minutes = %s, enabled = %s, source_status = %s,
                status_reason = %s, updated_at = now()
            WHERE source_id = %s
            """,
            (
                event.platform, event.allowed_domain, event.source_timezone, event.parser_version,
                event.interval_minutes, event.enabled, event.source_status, event.status_reason, local_id,
            ),
        )
    check_values = [
        event.effective_interval_minutes, event.last_attempt_at, event.last_attempt_status,
        event.last_success_at, event.next_due_at, event.checked_through, event.last_error_class,
    ]
    if any(value is not None for value in check_values):
        if event.effective_interval_minutes is None:
            raise SyncStoreError("来源检查状态缺少 effective_interval_minutes")
        cursor.execute(
            """
            INSERT INTO watchlist_announcement_check_state (
                market, issuer_identifier, source_id, effective_interval_minutes,
                last_attempt_at, last_attempt_status, last_success_at, next_due_at,
                checked_through, last_error_class
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (market, issuer_identifier, source_id) DO UPDATE SET
                effective_interval_minutes = EXCLUDED.effective_interval_minutes,
                last_attempt_at = EXCLUDED.last_attempt_at,
                last_attempt_status = EXCLUDED.last_attempt_status,
                last_success_at = EXCLUDED.last_success_at, next_due_at = EXCLUDED.next_due_at,
                checked_through = EXCLUDED.checked_through, last_error_class = EXCLUDED.last_error_class,
                updated_at = now()
            """,
            (
                event.market, event.issuer_identifier, local_id, event.effective_interval_minutes,
                event.last_attempt_at, event.last_attempt_status, event.last_success_at,
                event.next_due_at, event.checked_through, event.last_error_class,
            ),
        )
    else:
        cursor.execute("DELETE FROM watchlist_announcement_check_state WHERE source_id = %s", (local_id,))
    cursor.execute(
        """
        INSERT INTO hl_news_sync_source_state (
            local_source_id, open_gaps, earliest_gap_start, sync_seq
        ) VALUES (%s, %s, %s, %s)
        ON CONFLICT (local_source_id) DO UPDATE SET
            open_gaps = EXCLUDED.open_gaps, earliest_gap_start = EXCLUDED.earliest_gap_start,
            sync_seq = EXCLUDED.sync_seq, updated_at = now()
        """,
        (local_id, event.open_gaps, event.earliest_gap_start, event.sync_seq),
    )


def _local_source(cursor, source_instance: str, upstream_id: int) -> int:
    cursor.execute(
        "SELECT local_source_id FROM hl_news_sync_source_map WHERE source_instance = %s AND upstream_source_id = %s",
        (source_instance, upstream_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise SyncStoreError("条目事件缺少来源映射")
    return _scalar(row)


def _revision_row(cursor, source_id: int, record_id: str, revision: int) -> dict | None:
    cursor.execute(
        "SELECT * FROM watchlist_announcement WHERE source_id = %s AND source_record_id = %s AND revision = %s",
        (source_id, record_id, revision),
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _check_original(row: dict, event) -> None:
    fields = (
        "market", "issuer_identifier", "source_record_id", "title", "form_type", "original_url",
        "published_at", "published_at_precision", "source_published_raw", "source_timezone",
        "first_observed_at", "content_hash", "excerpt", "content_nature", "media_source", "is_backfill",
    )
    for field in fields:
        if row[field] != getattr(event, field):
            raise SyncStoreError(f"同一公告 revision 的原始字段冲突: {field}")


def _apply_relations(cursor, source_id: int, record_id: str, relations) -> None:
    cursor.execute(
        "SELECT id FROM watchlist_announcement WHERE source_id = %s AND source_record_id = %s ORDER BY revision LIMIT 1",
        (source_id, record_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise SyncStoreError("关联缺少本地公告锚点")
    announcement_id = _scalar(row)
    cursor.execute("DELETE FROM watchlist_announcement_security WHERE announcement_id = %s", (announcement_id,))
    seen = set()
    for relation in relations:
        key = (relation.market, relation.stock_code)
        if key in seen:
            raise SyncStoreError("公告关联包含重复证券")
        seen.add(key)
        cursor.execute(
            "INSERT INTO watchlist_announcement_security (announcement_id, market, stock_code, relation_reason) VALUES (%s, %s, %s, %s)",
            (announcement_id, relation.market, relation.stock_code, relation.relation_reason),
        )


def _insert_item(cursor, source_id: int, event) -> None:
    cursor.execute(
        """
        INSERT INTO watchlist_announcement (
            market, issuer_identifier, source_id, source_record_id, title, form_type,
            original_url, published_at, published_at_precision, source_published_raw,
            source_timezone, first_observed_at, last_seen_at, content_hash, excerpt,
            revision, is_backfill, content_nature, media_source, title_zh, summary_zh, importance
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event.market, event.issuer_identifier, source_id, event.source_record_id, event.title,
            event.form_type, event.original_url, event.published_at, event.published_at_precision,
            event.source_published_raw, event.source_timezone, event.first_observed_at,
            event.last_seen_at, event.content_hash, event.excerpt, event.revision, event.is_backfill,
            event.content_nature, event.media_source, event.title_zh, event.summary_zh, event.importance,
        ),
    )


def _apply_item(cursor, source_instance: str, event, bootstrap_status: str) -> None:
    source_id = _local_source(cursor, source_instance, event.source_id)
    row = _revision_row(cursor, source_id, event.source_record_id, event.revision)
    if row is None:
        cursor.execute(
            "SELECT max(revision) FROM watchlist_announcement WHERE source_id = %s AND source_record_id = %s",
            (source_id, event.source_record_id),
        )
        maximum = _scalar(cursor.fetchone())
        if maximum is not None and event.revision != maximum + 1:
            raise SyncStoreError("公告只能在已有最高 revision 后逐项追加")
        if maximum is None and bootstrap_status == "completed" and event.revision != 1:
            raise SyncStoreError("初始化完成后，新逻辑公告必须从 revision=1 开始")
        if bootstrap_status not in {"in_progress", "completed"}:
            raise SyncStoreError("同步状态不允许接收公告历史")
        _insert_item(cursor, source_id, event)
        row = _revision_row(cursor, source_id, event.source_record_id, event.revision)
    else:
        _check_original(row, event)
    if event.event_kind == "announcement":
        cursor.execute(
            "UPDATE watchlist_announcement SET last_seen_at = %s WHERE id = %s",
            (event.last_seen_at, row["id"]),
        )
    elif event.event_kind == "relation":
        cursor.execute(
            "UPDATE watchlist_announcement SET last_seen_at = %s WHERE id = %s",
            (event.last_seen_at, row["id"]),
        )
    elif event.event_kind == "llm":
        cursor.execute(
            """
            UPDATE watchlist_announcement SET title_zh = %s, summary_zh = %s,
                importance = %s, last_seen_at = %s WHERE id = %s
            """,
            (event.title_zh, event.summary_zh, event.importance, event.last_seen_at, row["id"]),
        )
    _apply_relations(cursor, source_id, event.source_record_id, event.relations)


def apply_page(url: str, response, expected_instance: str, bootstrap: bool = False) -> dict:
    if response.source_instance != expected_instance:
        raise SyncStoreError("上游 source_instance 与本地绑定不一致")
    events = sorted([*response.sources, *response.items], key=lambda event: event.sync_seq)
    if len({event.sync_seq for event in events}) != len(events):
        raise SyncStoreError("本页包含重复 sync_seq")
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            state = _check_page_allowed(cursor, bootstrap)
            if state["source_instance"] is not None and state["source_instance"] != expected_instance:
                raise SyncStoreError("本地已绑定其他上游实例")
            replayed = 0
            for event in events:
                digest = _hash_event(event)
                cursor.execute(
                    "SELECT * FROM hl_news_sync_event_receipt WHERE source_instance = %s AND sync_seq = %s",
                    (expected_instance, event.sync_seq),
                )
                receipt = cursor.fetchone()
                if receipt is not None:
                    if receipt["event_kind"] != event.event_kind or receipt["payload_sha256"] != digest:
                        raise SyncStoreError("同一 sync_seq 的事件内容或类型发生冲突")
                    replayed += 1
                    continue
                if event.event_kind == "source":
                    _apply_source(cursor, expected_instance, event)
                else:
                    _apply_item(cursor, expected_instance, event, "in_progress" if bootstrap else state["bootstrap_status"])
                cursor.execute(
                    "INSERT INTO hl_news_sync_event_receipt (source_instance, sync_seq, event_kind, payload_sha256) VALUES (%s, %s, %s, %s)",
                    (expected_instance, event.sync_seq, event.event_kind, digest),
                )
            last_seq = max((event.sync_seq for event in events), default=state["last_sync_seq"])
            bootstrap_status = "in_progress" if bootstrap else state["bootstrap_status"]
            completed_at = state["bootstrap_completed_at"]
            business_hash = state["bootstrap_business_hash"]
            if bootstrap and not response.has_more:
                bootstrap_status = "completed"
                completed_at = datetime.now(timezone.utc)
                business_hash = _business_hash(cursor)
            cursor.execute(
                """
                UPDATE hl_news_sync_state SET
                    source_instance = %s, next_cursor = %s, snapshot_at = %s,
                    last_sync_seq = %s, bootstrap_status = %s, bootstrap_completed_at = %s,
                    bootstrap_business_hash = %s, last_success_at = now(), last_error = NULL,
                    updated_at = now()
                WHERE singleton = true
                """,
                (
                    expected_instance, response.next_cursor, response.snapshot_at, last_seq,
                    bootstrap_status, completed_at, business_hash,
                ),
            )
    return {"events": len(events), "replayed": replayed, "has_more": response.has_more, "bootstrap_status": bootstrap_status}


def mark_attempt(url: str) -> None:
    with connect(url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE hl_news_sync_state SET last_attempt_at = now(), updated_at = now() WHERE singleton = true")


def mark_error(url: str, error: Exception) -> None:
    detail = error.detail if hasattr(error, "detail") else str(error)
    with connect(url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE hl_news_sync_state SET last_error = %s, updated_at = now() WHERE singleton = true",
                (detail[:500],),
            )


def status(url: str) -> dict:
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            state = _state(cursor)
            counts = {}
            for table in ("hl_news_sync_event_receipt", "hl_news_sync_source_map"):
                cursor.execute(f"SELECT count(*) FROM {table}")
                counts[table] = _scalar(cursor.fetchone())
            cursor.execute("SELECT count(*) FROM watchlist_announcement_source WHERE ingest_mode = 'stock_data'")
            counts["sources"] = _scalar(cursor.fetchone())
            cursor.execute(
                "SELECT count(*) FROM watchlist_announcement a JOIN watchlist_announcement_source s ON s.source_id = a.source_id WHERE s.ingest_mode = 'stock_data'"
            )
            counts["revisions"] = _scalar(cursor.fetchone())
            cursor.execute(
                "SELECT count(DISTINCT (a.source_id, a.source_record_id)) FROM watchlist_announcement a JOIN watchlist_announcement_source s ON s.source_id = a.source_id WHERE s.ingest_mode = 'stock_data'"
            )
            counts["logical_announcements"] = _scalar(cursor.fetchone())
            return {
                "source_instance": state["source_instance"], "next_cursor_saved": state["next_cursor"] is not None,
                "snapshot_at": state["snapshot_at"], "last_sync_seq": state["last_sync_seq"],
                "bootstrap_status": state["bootstrap_status"],
                "bootstrap_completed_at": state["bootstrap_completed_at"],
                "last_attempt_at": state["last_attempt_at"], "last_success_at": state["last_success_at"],
                "last_error": state["last_error"], "counts": counts,
                "business_hash": _business_hash(cursor),
            }


def activate(url: str) -> dict:
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            _lock(cursor)
            _require_drained(cursor)
            state = _state(cursor)
            if state["bootstrap_status"] != "completed" or not state["bootstrap_business_hash"]:
                raise SyncStoreError("bootstrap 尚未完成，拒绝激活")
            if _business_hash(cursor) != state["bootstrap_business_hash"]:
                raise SyncStoreError("bootstrap 完成后业务数据已变化，拒绝激活")
            cursor.execute("UPDATE operation_control SET mode = 'active', drain_deadline = NULL, updated_at = now() WHERE singleton = true")
    return {"status": "active", "source_instance": state["source_instance"], "business_hash": state["bootstrap_business_hash"]}
