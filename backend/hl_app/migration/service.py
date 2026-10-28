from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from psycopg2 import sql

from .common import (
    ENTITIES,
    NATURAL_KEYS,
    BundleError,
    business_hash,
    canonical,
    digest,
    natural_key,
    read_bundle,
    rows_by_key,
    write_bundle,
)

OPERATION_LOCK_KEY = 0x484C5F4F50455231
SERIAL_TABLES = {
    "securities": "id",
    "watchlist_items": "id",
    "daily_quotes": "id",
    "watchlist_announcement_source": "source_id",
    "watchlist_announcement": "id",
    "watchlist_announcement_gap": "id",
    "watchlist_announcement_run": "id",
    "migration_id_map": "id",
}


def _connect(url: str):
    return psycopg2.connect(url, connect_timeout=10)


def _lock(cursor) -> None:
    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (OPERATION_LOCK_KEY,))


def _mode(cursor) -> str:
    cursor.execute("SELECT mode FROM operation_control WHERE singleton = true")
    row = cursor.fetchone()
    if row is None:
        raise BundleError("operation control 未初始化")
    return row[0]


def assert_active(url: str) -> None:
    with closing(_connect(url)) as conn:
        with conn.cursor() as cursor:
            _lock(cursor)
            if _mode(cursor) != "active":
                raise BundleError("应用处于维护状态，暂停业务写入")


def maintenance(url: str) -> dict:
    with closing(_connect(url)) as conn, conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (OPERATION_LOCK_KEY,))
            cursor.execute(
                "UPDATE operation_control SET mode = 'maintenance', drain_deadline = now() + interval '30 seconds', updated_at = now() WHERE singleton = true"
            )
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND pid <> pg_backend_pid()")
            active = cursor.fetchone()[0]
    return {"mode": "maintenance", "drained": True, "other_active_sessions_at_exit": active}


def _current_rows(conn) -> dict[str, list[dict]]:
    rows = {}
    with conn.cursor() as cursor:
        for entity in ENTITIES:
            cursor.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(entity)))
            columns = [description[0] for description in cursor.description]
            rows[entity] = [dict(zip(columns, row)) for row in cursor.fetchall()]
    return rows


def _empty_business(cursor) -> bool:
    for entity in ENTITIES:
        cursor.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {} LIMIT 1)").format(sql.Identifier(entity)))
        if cursor.fetchone()[0]:
            return False
    return True


def _insert_row(cursor, entity: str, row: dict) -> None:
    columns = list(row)
    cursor.execute(
        sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(entity),
            sql.SQL(", ").join(map(sql.Identifier, columns)),
            sql.SQL(", ").join(
                sql.SQL("%s::tstzrange[]") if column in {"covered_ranges", "completed_ranges", "pending_ranges"} else sql.Placeholder()
                for column in columns
            ),
        ),
        [row[column] for column in columns],
    )


def _validate_references(rows: dict[str, list[dict]]) -> None:
    security_ids = {row["id"] for row in rows["securities"]}
    security_keys = {(row["market"], row["stock_code"]) for row in rows["securities"]}
    source_ids = {row["source_id"] for row in rows["watchlist_announcement_source"]}
    announcement_ids = {row["id"] for row in rows["watchlist_announcement"]}
    if any(row["security_id"] not in security_ids for row in rows["watchlist_items"]):
        raise BundleError("watchlist 引用不存在的证券")
    if any((row["market"], row["stock_code"]) not in security_keys for row in rows["daily_quotes"]):
        raise BundleError("行情引用不存在的证券")
    if any(row["source_id"] not in source_ids for row in rows["watchlist_announcement"]):
        raise BundleError("公告引用不存在的来源")
    if any(row["announcement_id"] not in announcement_ids for row in rows["watchlist_announcement_security"]):
        raise BundleError("公告关联引用不存在的首版公告")
    for entity in ("watchlist_announcement_check_state", "watchlist_announcement_gap", "watchlist_announcement_recovery"):
        if any(row["source_id"] not in source_ids for row in rows[entity]):
            raise BundleError(f"{entity} 引用不存在的来源")
    if any(row["source_id"] is not None and row["source_id"] not in source_ids for row in rows["watchlist_announcement_run"]):
        raise BundleError("运行记录引用不存在的来源")


def inspect_bundle(path: Path) -> dict:
    manifest, rows, manifest_hash = read_bundle(path, "full", "stock_data_to_hl")
    if not manifest.get("quiesced"):
        raise BundleError("正式 full 必须来自停写一致性快照")
    _validate_references(rows)
    return {
        "bundle_id": manifest["bundle_id"],
        "manifest_sha256": manifest_hash,
        "business_sha256": business_hash(rows),
        "counts": {entity: len(rows[entity]) for entity in ENTITIES},
    }


def import_bundle(url: str, path: Path, apply: bool) -> dict:
    inspected = inspect_bundle(path)
    manifest, rows, manifest_hash = read_bundle(path, "full", "stock_data_to_hl")
    bundle_id = manifest["bundle_id"]
    if not apply:
        with closing(_connect(url)) as conn:
            with conn.cursor() as cursor:
                _lock(cursor)
                if _mode(cursor) != "maintenance":
                    raise BundleError("导入要求 maintenance")
                cursor.execute("SELECT bundle_id FROM migration_bundles WHERE bundle_id = %s", (bundle_id,))
                if cursor.fetchone():
                    raise BundleError("该 bundle 已导入，dry-run 不执行重复导入")
                if not _empty_business(cursor):
                    raise BundleError("full 只允许空业务库")
        return {**inspected, "status": "dry_run_ok"}
    with closing(_connect(url)) as conn, conn:
        with conn.cursor() as cursor:
            _lock(cursor)
            if _mode(cursor) != "maintenance":
                raise BundleError("导入要求 maintenance")
            cursor.execute("SELECT manifest_sha256 FROM migration_bundles WHERE bundle_id = %s", (bundle_id,))
            existing = cursor.fetchone()
            if existing:
                if existing[0] != manifest_hash:
                    raise BundleError("同 bundle_id 内容已变化")
                current_hash = business_hash(_current_rows(conn))
                if current_hash != inspected["business_sha256"]:
                    raise BundleError("导入后已有变化，拒绝用旧包覆盖")
                return {**inspected, "status": "already_imported"}
            cursor.execute("SELECT COUNT(*) FROM migration_bundles")
            if cursor.fetchone()[0]:
                raise BundleError("目标已有其他成功 full 包")
            if not _empty_business(cursor):
                raise BundleError("full 只允许空业务库")
            for entity in ENTITIES:
                for row in rows[entity]:
                    _insert_row(cursor, entity, row)
                    cursor.execute(
                        "INSERT INTO migration_id_map (bundle_id, entity, source_id, natural_key, target_id) VALUES (%s, %s, %s, %s, %s)",
                        (bundle_id, entity, str(row.get("id", row.get("source_id", natural_key(entity, row)))), json.dumps(natural_key(entity, row)), str(row.get("id", row.get("source_id", "")))),
                    )
            for table, column in SERIAL_TABLES.items():
                cursor.execute(
                    sql.SQL("SELECT setval(pg_get_serial_sequence(%s, %s), COALESCE(MAX({}), 1), MAX({}) IS NOT NULL) FROM {}").format(
                        sql.Identifier(column), sql.Identifier(column), sql.Identifier(table)
                    ),
                    (table, column),
                )
            cursor.execute(
                "INSERT INTO migration_bundles (bundle_id, kind, direction, manifest_sha256, business_sha256, status, scope) VALUES (%s, 'full', 'stock_data_to_hl', %s, %s, 'success', %s)",
                (bundle_id, manifest_hash, inspected["business_sha256"], json.dumps(manifest.get("scope", {}))),
            )
    return {**inspected, "status": "imported"}


def _full_lineage(cursor) -> tuple[str, str, dict]:
    cursor.execute("SELECT bundle_id, manifest_sha256, scope FROM migration_bundles ORDER BY created_at DESC LIMIT 1")
    row = cursor.fetchone()
    if row is None:
        raise BundleError("缺少成功 full lineage")
    return row[0], row[1], row[2]


def export_baseline(url: str, output: Path) -> dict:
    output = output.resolve()
    with closing(_connect(url)) as conn, conn:
        with conn.cursor() as cursor:
            _lock(cursor)
            if _mode(cursor) != "maintenance":
                raise BundleError("基线导出要求 maintenance")
            full_id, full_hash, scope = _full_lineage(cursor)
            rows = _current_rows(conn)
            content_hash = business_hash(rows)
            baseline_id = str(uuid.uuid4())
            manifest = {
                "bundle_id": baseline_id,
                "bundle_version": 1,
                "kind": "baseline",
                "direction": "hl_snapshot",
                "source_instance": os.environ.get("HL_APP_INSTANCE_ID", "hl-app-local"),
                "source_commit": "hl-trader",
                "source_schema": 6,
                "scope": scope,
                "cutoff_at": datetime.now(timezone.utc).isoformat(),
                "snapshot_id": baseline_id,
                "quiesced": True,
                "baseline_id": baseline_id,
                "baseline_hash": content_hash,
                "full_bundle_id": full_id,
                "full_manifest_sha256": full_hash,
            }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".baseline-", dir=output.parent))
    try:
        write_bundle(temporary, manifest, rows)
        read_bundle(temporary, "baseline", "hl_snapshot")
        if output.exists():
            raise BundleError("基线输出目录已存在")
        temporary.rename(output)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    manifest_hash = __import__("hashlib").sha256((output / "manifest.json").read_bytes()).hexdigest()
    with closing(_connect(url)) as conn, conn:
        with conn.cursor() as cursor:
            _lock(cursor)
            cursor.execute(
                "INSERT INTO baseline_ledger (baseline_id, manifest_sha256, business_sha256, target_instance, full_bundle_id, full_manifest_sha256, scope) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (baseline_id, manifest_hash, content_hash, manifest["source_instance"], full_id, full_hash, json.dumps(scope)),
            )
            cursor.execute(
                "INSERT INTO export_ledger (export_id, purpose, baseline_id, content_sha256) VALUES (%s, 'baseline', %s, %s)",
                (str(uuid.uuid4()), baseline_id, content_hash),
            )
    return {"baseline_id": baseline_id, "business_sha256": content_hash, "output": str(output)}


def activate(url: str, baseline_path: Path) -> dict:
    manifest, rows, manifest_hash = read_bundle(baseline_path, "baseline", "hl_snapshot")
    baseline_id = manifest["baseline_id"]
    with closing(_connect(url)) as conn, conn:
        with conn.cursor() as cursor:
            _lock(cursor)
            cursor.execute("SELECT manifest_sha256, business_sha256, cutover_at FROM baseline_ledger WHERE baseline_id = %s", (baseline_id,))
            ledger = cursor.fetchone()
            if ledger is None or ledger[0] != manifest_hash or ledger[1] != business_hash(rows):
                raise BundleError("基线未注册或内容不匹配")
            if ledger[2] is not None:
                return {"baseline_id": baseline_id, "mode": "active", "already_active": True}
            if _mode(cursor) != "maintenance":
                raise BundleError("只允许从 maintenance 激活")
            if business_hash(_current_rows(conn)) != ledger[1]:
                raise BundleError("基线后数据已变化，拒绝激活")
            cursor.execute(
                "UPDATE operation_control SET mode = 'active', cutover_baseline_id = %s, drain_deadline = NULL, updated_at = now() WHERE singleton = true",
                (baseline_id,),
            )
            cursor.execute("UPDATE baseline_ledger SET cutover_at = now() WHERE baseline_id = %s", (baseline_id,))
    return {"baseline_id": baseline_id, "mode": "active"}


def rollback_export(url: str, baseline_path: Path, output: Path) -> dict:
    manifest, baseline_rows, baseline_hash = read_bundle(baseline_path, "baseline", "hl_snapshot")
    baseline_id = manifest["baseline_id"]
    with closing(_connect(url)) as conn, conn:
        with conn.cursor() as cursor:
            _lock(cursor)
            if _mode(cursor) != "maintenance":
                raise BundleError("回退导出要求 maintenance")
            cursor.execute("SELECT cutover_baseline_id FROM operation_control WHERE singleton = true")
            if cursor.fetchone()[0] != baseline_id:
                raise BundleError("不是当前注册的 cutover baseline")
            cursor.execute("SELECT manifest_sha256, business_sha256 FROM baseline_ledger WHERE baseline_id = %s", (baseline_id,))
            ledger = cursor.fetchone()
            if ledger is None or ledger[0] != baseline_hash or ledger[1] != business_hash(baseline_rows):
                raise BundleError("基线 ledger 与文件不匹配")
            current = _current_rows(conn)
            cutoff = datetime.now(timezone.utc).isoformat()
    changes = []
    for entity in ENTITIES:
        before = rows_by_key(entity, baseline_rows[entity])
        after = rows_by_key(entity, current[entity])
        for key in sorted(set(before) | set(after)):
            old, new = before.get(key), after.get(key)
            if old == new:
                continue
            op = "insert" if old is None else "delete" if new is None else "update"
            changes.append({
                "entity": entity,
                "natural_key": json.loads(key),
                "op": op,
                "before": old,
                "after": new,
                "before_sha256": digest(old) if old is not None else None,
                "after_sha256": digest(new) if new is not None else None,
            })
    output = output.resolve()
    if output.exists():
        raise BundleError("回退输出目录已存在")
    output.mkdir(parents=True, mode=0o700)
    snapshot_manifest = {
        **manifest,
        "bundle_id": str(uuid.uuid4()),
        "kind": "rollback_snapshot",
        "cutoff_at": cutoff,
        "snapshot_id": str(uuid.uuid4()),
    }
    write_bundle(output / "rollback_snapshot", snapshot_manifest, current)
    delta_id = str(uuid.uuid4())
    delta_manifest = {
        **manifest,
        "bundle_id": delta_id,
        "kind": "rollback_delta",
        "direction": "hl_to_stock_data",
        "cutoff_at": cutoff,
        "snapshot_id": delta_id,
        "rollback_snapshot": snapshot_manifest["snapshot_id"],
        "change_count": len(changes),
    }
    delta_dir = output / "rollback_delta"
    delta_dir.mkdir(mode=0o700)
    raw = "".join(canonical(change) + "\n" for change in changes).encode("utf-8")
    (delta_dir / "changes.jsonl").write_bytes(raw)
    import hashlib
    (delta_dir / "manifest.json").write_text(canonical({**delta_manifest, "files": [{"path": "changes.jsonl", "records": len(changes), "sha256": hashlib.sha256(raw).hexdigest(), "encoding": "utf-8-jsonl"}]}) + "\n", encoding="utf-8")
    (output / "report.json").write_text(canonical({"baseline_id": baseline_id, "changes": len(changes), "current_business_sha256": business_hash(current)}) + "\n", encoding="utf-8")
    return {"baseline_id": baseline_id, "changes": len(changes), "output": str(output)}


def business_write_guard(cursor) -> None:
    _lock(cursor)
    if _mode(cursor) != "active":
        raise BundleError("应用处于维护状态，暂停业务写入")
