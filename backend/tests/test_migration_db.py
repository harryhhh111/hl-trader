from __future__ import annotations

import json
import shutil
from pathlib import Path

import psycopg2
import pytest

from hl_app.db import migrate
from hl_app.migration.common import BundleError, read_bundle
from hl_app.migration.reference import RecoveryConflict, apply_rollback_delta
from hl_app.migration.service import (
    activate,
    export_baseline,
    import_bundle,
    inspect_bundle,
    maintenance,
    rollback_export,
)
from hl_app.services.watchlist import update_item

from .conftest import PostgresNamespace

pytestmark = pytest.mark.db
FULL = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "migration" / "full"


@pytest.fixture
def ready(postgres_namespace: PostgresNamespace):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    return postgres_namespace


def clone_bundle(tmp_path: Path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(FULL, target)
    return target


def test_full_import_dry_run_apply_and_idempotent_rerun(ready) -> None:
    assert inspect_bundle(FULL)["counts"]["watchlist_announcement"] == 2
    assert import_bundle(ready.migration_url, FULL, False)["status"] == "dry_run_ok"
    assert import_bundle(ready.migration_url, FULL, True)["status"] == "imported"
    assert import_bundle(ready.migration_url, FULL, True)["status"] == "already_imported"
    with psycopg2.connect(ready.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM watchlist_announcement")
            assert cursor.fetchone()[0] == 2
            cursor.execute("SELECT COUNT(*) FROM migration_id_map")
            assert cursor.fetchone()[0] == 14
            cursor.execute("SELECT COUNT(*) FROM migration_bundles")
            assert cursor.fetchone()[0] == 1


def test_changed_bundle_and_nonempty_target_are_rejected(ready, tmp_path) -> None:
    import_bundle(ready.migration_url, FULL, True)
    changed = clone_bundle(tmp_path)
    manifest_path = changed / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["snapshot_id"] = "different-snapshot"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match="内容已变化|其他成功|空业务库"):
        import_bundle(ready.migration_url, changed, True)


def test_bad_reference_fails_before_import_and_rolls_back(ready, tmp_path) -> None:
    bundle = clone_bundle(tmp_path)
    path = bundle / "watchlist_announcement_security.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["announcement_id"] = 999
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    import hashlib
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["files"]:
        if item["entity"] == "watchlist_announcement_security":
            item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleError, match="首版公告"):
        import_bundle(ready.migration_url, bundle, True)
    with psycopg2.connect(ready.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM watchlist_announcement")
            assert cursor.fetchone()[0] == 0


def test_baseline_activate_rollback_and_reference_recovery(ready, tmp_path) -> None:
    import_bundle(ready.migration_url, FULL, True)
    baseline_path = tmp_path / "baseline"
    baseline = export_baseline(ready.migration_url, baseline_path)
    assert activate(ready.migration_url, baseline_path)["mode"] == "active"
    assert activate(ready.migration_url, baseline_path)["already_active"] is True
    update_item(
        ready.runtime_url,
        1,
        {"group_name": "新分组", "note": "切换后理由", "research_url": "https://example.com/new", "sort_order": 3},
    )
    _, full_rows, _ = read_bundle(FULL)
    with psycopg2.connect(ready.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE watchlist_items SET enabled = false WHERE id = 2")
            cursor.execute(
                """
                INSERT INTO watchlist_announcement (
                    id, market, issuer_identifier, source_id, source_record_id, title,
                    form_type, original_url, published_at, published_at_precision,
                    source_published_raw, source_timezone, first_observed_at,
                    last_seen_at, captured_at, content_hash, excerpt, content_nature,
                    relation_reason, media_source, revision, is_backfill,
                    title_zh, summary_zh, importance
                ) SELECT 3, market, issuer_identifier, source_id, source_record_id,
                    '第三版', form_type, original_url, published_at, published_at_precision,
                    source_published_raw, source_timezone, first_observed_at,
                    last_seen_at, captured_at, 'three', excerpt, content_nature,
                    relation_reason, media_source, 3, is_backfill,
                    title_zh, summary_zh, importance FROM watchlist_announcement WHERE id = 2
                """
            )
            cursor.execute(
                "INSERT INTO daily_quotes (market, stock_code, trade_date, close, source, currency) VALUES ('US', 'MU', '2026-10-22', 100, 'fixture', 'USD')"
            )
            cursor.execute("DELETE FROM watchlist_announcement_gap WHERE id = 1")
    assert maintenance(ready.migration_url)["drained"] is True
    output = tmp_path / "rollback"
    result = rollback_export(ready.migration_url, baseline_path, output)
    assert result["changes"] >= 5
    changes = [json.loads(line) for line in (output / "rollback_delta" / "changes.jsonl").read_text().splitlines()]
    assert {change["op"] for change in changes} == {"insert", "update", "delete"}
    assert any(change["entity"] == "watchlist_announcement" and change["after"]["revision"] == 3 for change in changes if change["op"] == "insert")
    assert any(change["entity"] == "daily_quotes" for change in changes)
    snapshot_manifest = json.loads((output / "rollback_snapshot" / "manifest.json").read_text())
    assert snapshot_manifest["kind"] == "rollback_snapshot"
    assert snapshot_manifest["baseline_id"] == baseline["baseline_id"]

    recovered = apply_rollback_delta(full_rows, changes)
    assert recovered["watchlist_items"][0]["note"] == "切换后理由"
    assert recovered["watchlist_items"][1]["enabled"] is False
    assert len(recovered["watchlist_announcement"]) == 3
    assert not recovered["watchlist_announcement_gap"]
    apply_rollback_delta(recovered, changes)
    conflict_rows = {entity: [dict(row) for row in rows] for entity, rows in full_rows.items()}
    conflict_rows["watchlist_items"][0]["note"] = "源侧并发修改"
    with pytest.raises(RecoveryConflict):
        apply_rollback_delta(conflict_rows, changes)
