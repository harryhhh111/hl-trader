from __future__ import annotations

import hashlib
import json
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen

from psycopg2.extras import RealDictCursor

from ..db import connect
from .announcements import (
    AnnouncementRecord,
    delete_recovery,
    insert_run,
    load_recovery,
    record_attempt,
    record_gap,
    record_success,
    update_llm_fields,
    upsert_announcement,
    upsert_recovery,
)
from .ir import parse_ir
from .sec_client import SECClient


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _text(element, name: str) -> str:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == name:
            return (child.text or "").strip()
    return ""


def _link(element) -> str:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] == "link":
            if child.text and child.text.strip():
                return child.text.strip()
            if child.attrib.get("href"):
                return child.attrib["href"]
    return ""


def parse_feed(payload: bytes, source: dict) -> list[AnnouncementRecord]:
    root = ET.fromstring(payload)
    elements = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    records = []
    for element in elements:
        title = _text(element, "title")
        url = _link(element)
        stable = _text(element, "guid") or _text(element, "id") or url
        raw_time = _text(element, "pubDate") or _text(element, "published") or _text(element, "updated")
        published = None
        precision = "unknown"
        if raw_time:
            try:
                published = _utc(parsedate_to_datetime(raw_time))
                precision = "second"
            except (TypeError, ValueError):
                try:
                    published = _utc(datetime.fromisoformat(raw_time.replace("Z", "+00:00")))
                    precision = "second"
                except ValueError:
                    published = None
        if not title or not url or not stable:
            continue
        excerpt = _text(element, "description") or _text(element, "summary") or None
        content_hash = hashlib.sha256(
            json.dumps([stable, title, url, raw_time, excerpt], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        records.append(
            AnnouncementRecord(
                source_record_id=stable,
                form_type="press_release" if source["source_kind"] == "ir" else "media_report",
                title=title,
                url=url,
                published_at=published,
                published_precision=precision,
                published_raw=raw_time,
                source_timezone=source.get("source_timezone") or "UTC",
                content_hash=content_hash,
                excerpt=excerpt,
                content_nature=None if source["source_kind"] == "ir" else "unclassified",
                media_source=None if source["source_kind"] == "ir" else (source.get("platform") or "unknown"),
            )
        )
    return records


def parse_sec(payload: bytes, source: dict) -> list[AnnouncementRecord]:
    data = json.loads(payload)
    recent = data.get("filings", {}).get("recent", {})
    fields = ["accessionNumber", "form", "acceptanceDateTime", "filingDate", "primaryDocument", "primaryDocDescription"]
    rows = zip(*(recent.get(field, []) for field in fields))
    records = []
    for accession, form, accepted, filed, document, description in rows:
        if not accession or not document:
            continue
        raw_time = accepted or filed or ""
        published = None
        precision = "unknown"
        if accepted:
            published = _utc(datetime.fromisoformat(accepted.replace("Z", "+00:00")))
            precision = "second"
        elif filed:
            precision = "day"
        cik = source["issuer_identifier"]
        accession_compact = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_compact}/{document}"
        title = description or f"SEC {form} {accession}"
        content_hash = hashlib.sha256(
            json.dumps([accession, form, raw_time, document, title], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        records.append(
            AnnouncementRecord(
                source_record_id=accession,
                form_type=form,
                title=title,
                url=url,
                published_at=published,
                published_precision=precision,
                published_raw=raw_time,
                source_timezone="UTC",
                content_hash=content_hash,
            )
        )
    return records


def parse_finnhub(payload: bytes, source: dict) -> list[AnnouncementRecord]:
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise ValueError("Finnhub 响应不是列表")
    records = []
    for row in rows:
        item_id, title = row.get("id"), (row.get("headline") or "").strip()
        if not item_id or not title:
            raise ValueError("Finnhub 条目缺 id/headline")
        timestamp = row.get("datetime")
        published = datetime.fromtimestamp(int(timestamp), tz=timezone.utc) if timestamp else None
        raw = str(timestamp or "")
        records.append(
            AnnouncementRecord(
                source_record_id=str(item_id),
                form_type="media_report",
                title=title,
                url=row.get("url") or "",
                published_at=published,
                published_precision="second" if published else "unknown",
                published_raw=raw,
                source_timezone="UTC",
                content_hash=hashlib.sha256(json.dumps([item_id, title, raw, row.get("source")], ensure_ascii=False).encode("utf-8")).hexdigest(),
                excerpt=(row.get("summary") or "").strip() or None,
                content_nature="unclassified",
                media_source=(row.get("source") or "").strip() or "unknown",
            )
        )
    return records


def _relations(conn, source: dict) -> list[tuple[str, str | None]]:
    kind = source["source_kind"]
    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        if kind in {"sec", "ir"}:
            cursor.execute(
                """
                SELECT s.stock_code FROM securities s JOIN watchlist_items w ON w.security_id = s.id
                WHERE s.market = 'US' AND s.cik = %s AND w.enabled = true ORDER BY s.stock_code
                """,
                (source["issuer_identifier"],),
            )
            return [(row["stock_code"], f"CIK {source['issuer_identifier']} 直连") for row in cursor.fetchall()]
        config = json.loads(source.get("query_config") or "{}")
        configured = config.get("relations", [])
        codes = [item.get("stock_code") for item in configured if item.get("stock_code")]
        if not codes:
            return []
        cursor.execute(
            "SELECT stock_code FROM securities s JOIN watchlist_items w ON w.security_id = s.id WHERE s.market = 'US' AND stock_code = ANY(%s) AND w.enabled = true",
            (codes,),
        )
        enabled = {row["stock_code"] for row in cursor.fetchall()}
        return [(item["stock_code"], item.get("reason")) for item in configured if item["stock_code"] in enabled]


def _fetch_http(url: str) -> bytes:
    headers = {"User-Agent": "hl-app-announcement-collector/1"}
    token = os.environ.get("HL_APP_MEDIA_CREDENTIALS", "").strip()
    if token:
        headers["X-Finnhub-Token"] = token
    request = Request(url, headers=headers)
    with urlopen(request, timeout=20) as response:
        return response.read(8 * 1024 * 1024 + 1)


def collect_source(
    url: str,
    source: dict,
    sec_client: SECClient | None = None,
    http_get=_fetch_http,
    now: datetime | None = None,
    enhancer=None,
) -> dict:
    started = now or datetime.now(timezone.utc)
    task_id = str(uuid.uuid4())
    issuer = source["issuer_identifier"]
    try:
        with connect(url) as conn:
            relations = _relations(conn, source)
        partial = False
        recovery_cursor = None
        recovery_start = started - timedelta(days=90)
        if source["source_kind"] == "sec":
            if sec_client is None:
                raise RuntimeError("SEC client is not configured")
            payload = sec_client.get(f"https://data.sec.gov/submissions/CIK{issuer}.json")
            records = parse_sec(payload, source)
            files = json.loads(payload).get("filings", {}).get("files", [])
            with connect(url) as conn:
                previous = load_recovery(conn, source["source_id"], issuer)
            offset = int(previous[2]) if previous and previous[3] else 0
            selected = files[offset:offset + 2]
            for file_info in selected:
                records.extend(parse_sec(sec_client.get(f"https://data.sec.gov/submissions/{file_info['name']}"), source))
            next_offset = offset + len(selected)
            partial = next_offset < len(files)
            recovery_cursor = str(next_offset)
            if files:
                recovery_start = datetime.fromisoformat(files[-1]["filingFrom"] + "T00:00:00+00:00")
        elif source["source_kind"] == "ir":
            records = parse_ir(http_get(source["entry_url"]), source)
        elif source.get("platform") == "finnhub":
            records = parse_finnhub(http_get(source["entry_url"]), source)
        else:
            records = parse_feed(http_get(source["entry_url"]), source)
        inserted = revised = 0
        new_versions = []
        with connect(url) as conn:
            for record in records:
                outcome, revision = upsert_announcement(conn, source["source_id"], issuer, relations, record, False, started)
                inserted += outcome == "inserted"
                revised += outcome == "revised"
                if outcome != "seen":
                    new_versions.append((record, revision))
            if partial:
                record_attempt(conn, source["source_id"], issuer, "partial", "budget_exhausted")
                upsert_recovery(conn, source["source_id"], issuer, recovery_start, started, recovery_cursor, True)
            else:
                record_success(conn, source["source_id"], issuer, started, started + timedelta(minutes=source.get("interval_minutes") or 60))
                delete_recovery(conn, source["source_id"], issuer)
        enhancement_failed = 0
        if enhancer is not None:
            for record, revision in new_versions:
                try:
                    fields = enhancer(record, source["source_kind"], relations[0][0] if relations else "")
                    with connect(url) as conn:
                        update_llm_fields(conn, source["source_id"], record.source_record_id, revision, **fields)
                except Exception:
                    enhancement_failed += 1
        run_status = "partial" if partial or enhancement_failed else "success"
        with connect(url) as conn:
            if enhancement_failed:
                record_attempt(conn, source["source_id"], issuer, "partial", "enhance_failed")
            insert_run(
                conn, task_id, source["source_id"], issuer, run_status, started,
                fetched=len(records), inserted=inserted, revised=revised,
                parser_version=source["parser_version"],
                error_class="enhance_failed" if enhancement_failed else None,
                error_detail=f"{enhancement_failed} 条增强失败" if enhancement_failed else None,
            )
        return {
            "fetched": len(records),
            "inserted": inserted,
            "revised": revised,
            "partial": run_status == "partial",
            "enhancement_failed": enhancement_failed,
        }
    except Exception as exc:
        with connect(url) as conn:
            record_attempt(conn, source["source_id"], issuer, "failed", type(exc).__name__)
            record_gap(conn, source["source_id"], issuer, started - timedelta(hours=1), started, "fetch_failed", str(exc)[:500])
            insert_run(conn, task_id, source["source_id"], issuer, "failed", started, error_class=type(exc).__name__, error_detail=str(exc)[:500])
        raise


def registered_sources(url: str) -> list[dict]:
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM watchlist_announcement_source
                WHERE market = 'US' AND enabled = true AND source_status = 'enabled'
                ORDER BY source_kind, issuer_identifier, source_id
                """
            )
            return [dict(row) for row in cursor.fetchall()]


def ensure_sec_sources(url: str) -> int:
    with connect(url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT DISTINCT cik FROM securities s JOIN watchlist_items w ON w.security_id = s.id WHERE s.market = 'US' AND s.cik IS NOT NULL AND w.enabled = true"
            )
            ciks = [row[0] for row in cursor.fetchall()]
        created = 0
        for cik in ciks:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO watchlist_announcement_source (
                        market, source_kind, issuer_identifier, allowed_domain,
                        entry_url, source_timezone, enabled, source_status, status_reason
                    ) VALUES ('US', 'sec', %s, 'data.sec.gov', %s, 'UTC', false, 'pending_verification', '等待新服务器 SEC 出口验证')
                    ON CONFLICT (market, source_kind, issuer_identifier, entry_url) DO NOTHING
                    """,
                    (cik, f"https://data.sec.gov/submissions/CIK{cik}.json"),
                )
                created += cursor.rowcount
    return created
