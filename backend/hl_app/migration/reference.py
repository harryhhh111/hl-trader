from __future__ import annotations

from .common import canonical, digest


class RecoveryConflict(RuntimeError):
    pass


def apply_rollback_delta(source_rows: dict[str, list[dict]], changes: list[dict]) -> dict[str, list[dict]]:
    result = {entity: [dict(row) for row in rows] for entity, rows in source_rows.items()}
    for change in changes:
        entity = change["entity"]
        key = canonical(change["natural_key"])
        rows = result.setdefault(entity, [])
        index = next((position for position, row in enumerate(rows) if canonical({field: row[field] for field in change["natural_key"]}) == key), None)
        current = rows[index] if index is not None else None
        before, after = change["before"], change["after"]
        if current == after:
            continue
        if current != before:
            raise RecoveryConflict(f"源侧并发变化：{entity} {key}")
        if after is None:
            if index is not None:
                rows.pop(index)
        elif index is None:
            rows.append(dict(after))
        else:
            rows[index] = dict(after)
    return result


def change_report(changes: list[dict]) -> dict:
    return {
        "insert": sum(change["op"] == "insert" for change in changes),
        "update": sum(change["op"] == "update" for change in changes),
        "delete": sum(change["op"] == "delete" for change in changes),
        "content_sha256": digest(changes),
    }
