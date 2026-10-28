from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ENTITIES = (
    "securities",
    "watchlist_items",
    "daily_quotes",
    "watchlist_announcement_source",
    "watchlist_announcement",
    "watchlist_announcement_security",
    "watchlist_announcement_check_state",
    "watchlist_announcement_gap",
    "watchlist_announcement_run",
    "watchlist_announcement_recovery",
)
NATURAL_KEYS = {
    "securities": ("market", "stock_code"),
    "watchlist_items": ("security_id",),
    "daily_quotes": ("market", "stock_code", "trade_date"),
    "watchlist_announcement_source": ("market", "source_kind", "issuer_identifier", "entry_url"),
    "watchlist_announcement": ("source_id", "source_record_id", "revision"),
    "watchlist_announcement_security": ("announcement_id", "market", "stock_code"),
    "watchlist_announcement_check_state": ("market", "issuer_identifier", "source_id"),
    "watchlist_announcement_gap": ("id",),
    "watchlist_announcement_run": ("id",),
    "watchlist_announcement_recovery": ("market", "issuer_identifier", "source_id"),
}


class BundleError(RuntimeError):
    pass


def normalize(value):
    if hasattr(value, "lower") and hasattr(value, "upper") and hasattr(value, "_bounds"):
        from datetime import timezone

        def format_bound(bound):
            return bound.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00") if bound is not None else ""

        return f"{value._bounds[0]}{format_bound(value.lower)},{format_bound(value.upper)}{value._bounds[1]}"
    if isinstance(value, datetime):
        from datetime import timezone

        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


def canonical(value) -> str:
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def natural_key(entity: str, row: dict) -> dict:
    return {key: normalize(row[key]) for key in NATURAL_KEYS[entity]}


def rows_by_key(entity: str, rows: list[dict]) -> dict:
    result = {}
    for row in rows:
        key = canonical(natural_key(entity, row))
        if key in result:
            raise BundleError(f"{entity} 含重复自然键：{key}")
        result[key] = normalize(row)
    return result


def business_hash(rows: dict[str, list[dict]]) -> str:
    return digest({entity: rows_by_key(entity, rows.get(entity, [])) for entity in ENTITIES})


def read_bundle(path: Path, expected_kind: str | None = None, expected_direction: str | None = None) -> tuple[dict, dict[str, list[dict]], str]:
    root = path.resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError("manifest.json 不存在或不是合法 JSON") from exc
    if manifest.get("bundle_version") != 1 or not manifest.get("bundle_id") or not manifest.get("snapshot_id"):
        raise BundleError("bundle_version、bundle_id 或 snapshot_id 非法")
    if expected_kind is not None and manifest.get("kind") != expected_kind:
        raise BundleError(f"bundle kind 必须为 {expected_kind}")
    if expected_direction is not None and manifest.get("direction") != expected_direction:
        raise BundleError(f"bundle direction 必须为 {expected_direction}")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BundleError("manifest.files 必须是数组")
    rows: dict[str, list[dict]] = {}
    seen_paths = set()
    for entity in ENTITIES:
        matches = [item for item in files if item.get("entity") == entity]
        if len(matches) != 1:
            raise BundleError(f"缺少实体文件声明：{entity}")
        item = matches[0]
        relative = item.get("path")
        if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts or relative in seen_paths:
            raise BundleError("实体文件路径不安全或重复")
        seen_paths.add(relative)
        file_path = root / relative
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            raise BundleError(f"实体文件不可读：{relative}") from exc
        if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
            raise BundleError(f"实体文件校验和错误：{relative}")
        entity_rows = []
        for line in raw.decode("utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise BundleError(f"实体行不是 JSON 对象：{relative}")
                entity_rows.append(value)
        if len(entity_rows) != item.get("records"):
            raise BundleError(f"实体记录数错误：{relative}")
        rows_by_key(entity, entity_rows)
        rows[entity] = entity_rows
    if set(manifest.get("entities", [])) != set(ENTITIES):
        raise BundleError("manifest.entities 必须包含完整依赖闭包")
    manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return manifest, rows, manifest_hash


def write_bundle(path: Path, manifest: dict, rows: dict[str, list[dict]]) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = []
    for entity in ENTITIES:
        relative = f"{entity}.jsonl"
        raw = "".join(canonical(row) + "\n" for row in rows.get(entity, [])).encode("utf-8")
        (path / relative).write_bytes(raw)
        files.append({"entity": entity, "path": relative, "schema": 1, "encoding": "utf-8-jsonl", "records": len(rows.get(entity, [])), "sha256": hashlib.sha256(raw).hexdigest()})
    manifest = dict(manifest)
    manifest["entities"] = list(ENTITIES)
    manifest["files"] = files
    (path / "manifest.json").write_text(canonical(manifest) + "\n", encoding="utf-8")
