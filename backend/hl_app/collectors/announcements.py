from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class AnnouncementRecord:
    source_record_id: str
    form_type: str
    title: str
    url: str
    published_at: datetime | None
    published_precision: str
    published_raw: str
    source_timezone: str
    content_hash: str
    excerpt: str | None = None
    content_nature: str | None = None
    relation_reason: str | None = None
    media_source: str | None = None
    title_zh: str | None = None
    summary_zh: str | None = None
    importance: str | None = None


def _link(cursor, source_id: int, source_record_id: str, relations: list[tuple[str, str | None]]) -> None:
    for stock_code, reason in relations:
        cursor.execute(
            """
            INSERT INTO watchlist_announcement_security (announcement_id, market, stock_code, relation_reason)
            SELECT a.id, 'US', %s, %s FROM watchlist_announcement a
            WHERE a.source_id = %s AND a.source_record_id = %s AND a.revision = 1
              AND EXISTS (
                  SELECT 1 FROM securities s JOIN watchlist_items w ON w.security_id = s.id
                  WHERE s.market = 'US' AND s.stock_code = %s AND w.enabled = true
              )
            ON CONFLICT (announcement_id, market, stock_code) DO UPDATE SET
                relation_reason = COALESCE(watchlist_announcement_security.relation_reason, EXCLUDED.relation_reason)
            """,
            (stock_code, reason, source_id, source_record_id, stock_code),
        )


def upsert_announcement(
    conn,
    source_id: int,
    issuer: str,
    relations: list[tuple[str, str | None]],
    record: AnnouncementRecord,
    is_backfill: bool,
    observed_at: datetime,
) -> tuple[str, int]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, revision, content_hash FROM watchlist_announcement WHERE source_id = %s AND source_record_id = %s ORDER BY revision DESC LIMIT 1",
            (source_id, record.source_record_id),
        )
        existing = cursor.fetchone()
        if existing is not None and existing[2] == record.content_hash:
            cursor.execute("UPDATE watchlist_announcement SET last_seen_at = %s WHERE id = %s", (observed_at, existing[0]))
            _link(cursor, source_id, record.source_record_id, relations)
            return "seen", int(existing[1])
        revision = 1 if existing is None else int(existing[1]) + 1
        cursor.execute(
            """
            INSERT INTO watchlist_announcement (
                market, issuer_identifier, source_id, source_record_id, title,
                form_type, original_url, published_at, published_at_precision,
                source_published_raw, source_timezone, first_observed_at,
                last_seen_at, content_hash, excerpt, content_nature,
                relation_reason, media_source, revision, is_backfill,
                title_zh, summary_zh, importance
            ) VALUES ('US', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                issuer, source_id, record.source_record_id, record.title,
                record.form_type, record.url, record.published_at,
                record.published_precision, record.published_raw,
                record.source_timezone, observed_at, observed_at,
                record.content_hash, record.excerpt, record.content_nature,
                record.relation_reason, record.media_source, revision, is_backfill,
                record.title_zh, record.summary_zh, record.importance,
            ),
        )
        _link(cursor, source_id, record.source_record_id, relations)
    return ("inserted" if revision == 1 else "revised"), revision


def update_llm_fields(conn, source_id: int, source_record_id: str, revision: int, **fields) -> None:
    allowed = {"title_zh", "summary_zh", "importance"}
    values = [(key, value) for key, value in fields.items() if key in allowed and value is not None]
    if not values:
        return
    with conn.cursor() as cursor:
        cursor.execute(
            f"UPDATE watchlist_announcement SET {', '.join(f'{key} = %s' for key, _ in values)} WHERE source_id = %s AND source_record_id = %s AND revision = %s",
            (*[value for _, value in values], source_id, source_record_id, revision),
        )


def record_attempt(conn, source_id: int, issuer: str, status: str, error_class: str | None = None) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO watchlist_announcement_check_state (
                market, issuer_identifier, source_id, effective_interval_minutes,
                last_attempt_at, last_attempt_status, last_error_class
            ) VALUES ('US', %s, %s, 60, now(), %s, %s)
            ON CONFLICT (market, issuer_identifier, source_id) DO UPDATE SET
                last_attempt_at = now(), last_attempt_status = EXCLUDED.last_attempt_status,
                last_error_class = EXCLUDED.last_error_class, updated_at = now()
            """,
            (issuer, source_id, status, error_class),
        )


def record_success(conn, source_id: int, issuer: str, checked_through: datetime, next_due: datetime) -> None:
    record_attempt(conn, source_id, issuer, "success_new")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE watchlist_announcement_check_state SET last_success_at = now(),
                checked_through = %s, next_due_at = %s, last_error_class = NULL, updated_at = now()
            WHERE market = 'US' AND issuer_identifier = %s AND source_id = %s
            """,
            (checked_through, next_due, issuer, source_id),
        )


def record_gap(conn, source_id: int, issuer: str, start: datetime, end: datetime, reason: str, detail: str = "") -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO watchlist_announcement_gap (market, issuer_identifier, source_id, gap_range, reason, detail)
            VALUES ('US', %s, %s, tstzrange(%s, %s, '[)'), %s, %s)
            ON CONFLICT (market, issuer_identifier, source_id, gap_range, reason) WHERE resolved_at IS NULL DO NOTHING
            """,
            (issuer, source_id, start, end, reason, detail),
        )


def upsert_recovery(conn, source_id: int, issuer: str, start: datetime, target: datetime, cursor: str | None, applicable: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO watchlist_announcement_recovery (
                market, issuer_identifier, source_id, backfill_start, target_through,
                pending_ranges, cursor, cursor_applicable
            ) VALUES ('US', %s, %s, %s, %s, ARRAY[tstzrange(%s, %s, '[)')], %s, %s)
            ON CONFLICT (market, issuer_identifier, source_id) DO UPDATE SET
                pending_ranges = EXCLUDED.pending_ranges, cursor = EXCLUDED.cursor,
                cursor_applicable = EXCLUDED.cursor_applicable, updated_at = now()
            """,
            (issuer, source_id, start, target, start, target, cursor, applicable),
        )


def load_recovery(conn, source_id: int, issuer: str):
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT backfill_start, target_through, cursor, cursor_applicable FROM watchlist_announcement_recovery WHERE market = 'US' AND issuer_identifier = %s AND source_id = %s",
            (issuer, source_id),
        )
        return cursor.fetchone()


def delete_recovery(conn, source_id: int, issuer: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            "DELETE FROM watchlist_announcement_recovery WHERE market = 'US' AND issuer_identifier = %s AND source_id = %s",
            (issuer, source_id),
        )


def insert_run(conn, task_id: str, source_id: int, issuer: str, status: str, started_at: datetime, **counts) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO watchlist_announcement_run (
                task_id, market, issuer_identifier, source_id, started_at, finished_at,
                status, fetched_count, inserted_count, revised_count, error_class,
                error_detail, parser_version, dropped_count, trigger_kind
            ) VALUES (%s, 'US', %s, %s, %s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                task_id, issuer, source_id, started_at, status,
                counts.get("fetched"), counts.get("inserted"), counts.get("revised"),
                counts.get("error_class"), counts.get("error_detail"),
                counts.get("parser_version", 1), counts.get("dropped", 0),
                counts.get("trigger_kind", "scheduled"),
            ),
        )
