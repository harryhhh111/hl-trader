from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from psycopg2.extras import Json, RealDictCursor

from ..db import connect
from . import hyperliquid_accounts as accounts_service
from . import hyperliquid_adjustments as adjustments_service
from . import hyperliquid_pairs as pairs_service
from . import hyperliquid_risk as risk_service

STATUSES = {"disabled", "monitoring", "candidate", "executing", "cooling", "incomplete", "unknown", "unavailable"}
UNRESOLVED_STATUSES = {"executing", "incomplete", "unknown"}


class RebalanceError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _positive_decimal(value: str, label: str, maximum: Decimal) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, AttributeError, TypeError) as exc:
        raise RebalanceError(422, f"{label}必须是十进制数") from exc
    if not parsed.is_finite() or parsed <= 0 or parsed > maximum:
        raise RebalanceError(422, f"{label}必须大于 0 且不超过 {maximum}")
    return parsed


def _decimal_text(value) -> str | None:
    return None if value is None else format(Decimal(value), "f")


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _cooldown_seconds() -> int:
    try:
        value = int(os.environ.get("HL_APP_REBALANCE_COOLDOWN_SECONDS", "300"))
    except ValueError as exc:
        raise RebalanceError(503, "HL_APP_REBALANCE_COOLDOWN_SECONDS 配置非法") from exc
    if not 60 <= value <= 86400:
        raise RebalanceError(503, "HL_APP_REBALANCE_COOLDOWN_SECONDS 必须在 60 到 86400 之间")
    return value


def _scope_lock(cursor, rule: dict) -> None:
    key = f"rebalance:{rule.get('owner_account_id')}:{rule['network']}:{rule['pair_id']}:{rule['account_address']}"
    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))


def _rule_row(cursor, pair_id: str, account: str, owner_account_id: int | None, lock: bool = False) -> dict | None:
    cursor.execute(
        f"""
        SELECT * FROM hyperliquid_rebalance_rules
        WHERE pair_id = %s AND account_address = %s AND owner_account_id IS NOT DISTINCT FROM %s
        {'FOR UPDATE' if lock else ''}
        """,
        (pair_id, account, owner_account_id),
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _risk_blocked(cursor, rule: dict) -> bool:
    cursor.execute(
        """
        SELECT enabled, status, trigger_kind FROM hyperliquid_pair_risk_rules
        WHERE pair_id = %s AND account_address = %s AND owner_account_id IS NOT DISTINCT FROM %s
        """,
        (rule["pair_id"], rule["account_address"], rule.get("owner_account_id")),
    )
    row = cursor.fetchone()
    return bool(
        row and row["enabled"]
        and (row["trigger_kind"] is not None or row["status"] in {"executing", "incomplete", "unknown"})
    )


def _event(cursor, rule_id: int, event_type: str, evaluation: dict | None = None, **values) -> None:
    snapshot = None if evaluation is None else {
        "current_notionals": evaluation["current_notionals"],
        "drift_pct": evaluation["drift_pct"],
        "open_orders": evaluation["open_orders"],
        "position_state": evaluation["position_state"],
        "checked_at": evaluation["checked_at"].isoformat(),
    }
    cursor.execute(
        """
        INSERT INTO hyperliquid_rebalance_events (
            rule_id, event_type, leg_indices, snapshot, action_id, request_id, detail
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            rule_id, event_type, Json(values.get("leg_indices", [])),
            Json(snapshot) if snapshot is not None else None,
            values.get("action_id"), values.get("request_id"), values.get("detail"),
        ),
    )


def _view(row: dict | None, pair_id: str, account: str, events: list[dict] | None = None, risk_blocked: bool = False) -> dict:
    stale_seconds = risk_service._stale_seconds()
    if row is None:
        return {
            "configured": False, "pair_id": pair_id, "account_address": account,
            "enabled": False, "target_notional": None, "threshold_pct": None,
            "rule_revision": 0, "status": "disabled", "worker_available": False,
            "last_attempt_at": None, "last_checked_at": None, "consecutive_count": 0,
            "current_notionals": None, "drift_pct": None, "open_orders": None,
            "position_state": None, "candidate_leg_indices": [], "candidate_preview": None, "candidate_at": None,
            "cooldown_until": None, "last_action_id": None, "last_request_id": None,
            "last_error": None, "risk_blocked": risk_blocked, "events": [],
        }
    last_attempt = row["last_attempt_at"]
    worker_available = bool(
        row["enabled"] and last_attempt is not None
        and (datetime.now(timezone.utc) - last_attempt).total_seconds() <= stale_seconds
    )
    return {
        "configured": True,
        "pair_id": row["pair_id"],
        "account_address": row["account_address"],
        "network": row["network"],
        "pair_config_revision": row["pair_config_revision"],
        "enabled": row["enabled"],
        "target_notional": _decimal_text(row["target_notional"]),
        "threshold_pct": _decimal_text(row["threshold_pct"]),
        "rule_revision": row["revision"],
        "status": row["status"],
        "worker_available": worker_available,
        "last_attempt_at": _timestamp(last_attempt),
        "last_checked_at": _timestamp(row["last_checked_at"]),
        "consecutive_count": row["consecutive_count"],
        "current_notionals": row["current_notionals"],
        "drift_pct": row["drift_pct"],
        "open_orders": row["open_orders"],
        "position_state": row["position_state"],
        "candidate_leg_indices": row["candidate_leg_indices"],
        "candidate_preview": row["candidate_preview"],
        "candidate_at": _timestamp(row["candidate_at"]),
        "cooldown_until": _timestamp(row["cooldown_until"]),
        "last_action_id": row["last_action_id"],
        "last_request_id": None if row["last_request_id"] is None else str(row["last_request_id"]),
        "last_error": row["last_error"],
        "risk_blocked": risk_blocked,
        "updated_at": _timestamp(row["updated_at"]),
        "events": [
            {
                "id": event["id"], "event_type": event["event_type"],
                "leg_indices": event["leg_indices"], "snapshot": event["snapshot"],
                "action_id": event["action_id"],
                "request_id": None if event["request_id"] is None else str(event["request_id"]),
                "detail": event["detail"], "created_at": _timestamp(event["created_at"]),
            }
            for event in (events or [])
        ],
    }


def save_rule(
    url: str, pair_id: str, account_address: str, config_revision: str, enabled: bool,
    target_notional: str, threshold_pct: str, owner_account_id: int | None = None,
) -> dict:
    if type(enabled) is not bool:
        raise RebalanceError(422, "enabled 必须是布尔值")
    target = _positive_decimal(target_notional, "每腿目标名义", Decimal("1000000"))
    threshold = _positive_decimal(threshold_pct, "偏离阈值", Decimal("100"))
    descriptor, account = adjustments_service._resolve(
        url, pair_id, account_address, config_revision, owner_account_id,
    )
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            scope = {
                "owner_account_id": owner_account_id, "network": descriptor["network"],
                "pair_id": pair_id, "account_address": account,
            }
            _scope_lock(cursor, scope)
            existing = _rule_row(cursor, pair_id, account, owner_account_id, lock=True)
            revision = 1 if existing is None else existing["revision"] + 1
            status = "monitoring" if enabled else "disabled"
            values = (
                owner_account_id, descriptor["network"], pair_id, account, config_revision,
                enabled, target, threshold, status,
            )
            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO hyperliquid_rebalance_rules (
                        owner_account_id, network, pair_id, account_address, pair_config_revision,
                        enabled, target_notional, threshold_pct, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
                    """,
                    values,
                )
            else:
                cursor.execute(
                    """
                    UPDATE hyperliquid_rebalance_rules SET
                        pair_config_revision = %s, enabled = %s, target_notional = %s,
                        threshold_pct = %s, revision = %s, status = %s,
                        last_attempt_at = NULL, last_checked_at = NULL, consecutive_count = 0,
                        current_notionals = NULL, drift_pct = NULL, open_orders = NULL,
                        position_state = NULL, observed_leg_indices = '[]', candidate_leg_indices = '[]',
                        candidate_preview = NULL, candidate_at = NULL, cooldown_until = NULL, last_action_id = NULL,
                        last_request_id = NULL, last_error = NULL, updated_at = now()
                    WHERE id = %s RETURNING *
                    """,
                    (config_revision, enabled, target, threshold, revision, status, existing["id"]),
                )
            row = dict(cursor.fetchone())
            _event(cursor, row["id"], "rule_updated", detail=f"revision={revision}; enabled={enabled}")
            return _view(row, pair_id, account, risk_blocked=_risk_blocked(cursor, row))


def get_rule(url: str, pair_id: str, account_address: str, owner_account_id: int | None = None) -> dict:
    account = accounts_service.normalize_address(account_address)
    _revision, descriptors = pairs_service.load_pairs()
    descriptor = next((item for item in descriptors if item["id"] == pair_id), None)
    if descriptor is None:
        raise RebalanceError(404, "未知或停用的 pair")
    active = accounts_service.apply_active_accounts(url, [descriptor], owner_account_id)[0]["account_address"]
    if active != account:
        raise RebalanceError(409, "当前账户已变化，请刷新页面")
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            row = _rule_row(cursor, pair_id, account, owner_account_id)
            events = []
            if row is not None:
                cursor.execute(
                    "SELECT * FROM hyperliquid_rebalance_events WHERE rule_id = %s ORDER BY id DESC LIMIT 10",
                    (row["id"],),
                )
                events = [dict(item) for item in cursor.fetchall()]
            blocked = _risk_blocked(cursor, row or {
                "pair_id": pair_id, "account_address": account, "owner_account_id": owner_account_id,
            })
            return _view(row, pair_id, account, events, blocked)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp missing timezone")
    return parsed.astimezone(timezone.utc)


def evaluate_snapshot(snapshot: dict, rule: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    revision, descriptors = pairs_service.load_pairs()
    descriptor = next((item for item in descriptors if item["id"] == rule["pair_id"]), None)
    if descriptor is None:
        raise RebalanceError(409, "pair 已停用或配置已删除")
    pair = snapshot.get("pair") or {}
    if (
        snapshot.get("schema_version") != 2 or snapshot.get("config_revision") != revision
        or pair.get("id") != rule["pair_id"] or pair.get("network") != rule["network"]
        or pair.get("dex") != descriptor["dex"] or pair.get("coins") != descriptor["coins"]
        or str(pair.get("account_address") or "").lower() != rule["account_address"]
    ):
        raise RebalanceError(503, "监控快照身份或配置版本不一致")
    queried_at = _parse_time(snapshot["queried_at"])
    age = (now - queried_at).total_seconds()
    if age < -5 or age > risk_service._max_data_age_seconds():
        raise RebalanceError(503, "监控快照已过期或时间异常")
    positions = (snapshot.get("blocks") or {}).get("positions") or {}
    items = positions.get("items")
    open_orders = positions.get("open_orders")
    position_state = positions.get("position_state")
    if (
        positions.get("status") != "ok" or not isinstance(items, list)
        or type(open_orders) is not int or open_orders < 0
        or position_state not in {"flat", "single_leg", "opposite", "same_direction"}
    ):
        raise RebalanceError(503, "两腿持仓、名义或挂单数据不完整，本次不判断偏离")
    if position_state == "same_direction":
        raise RebalanceError(503, "当前为同向持仓，不是可再平衡的多空 pair")
    by_coin = {}
    for item in items:
        coin = item.get("coin")
        if coin not in descriptor["coins"] or coin in by_coin:
            raise RebalanceError(503, "持仓腿身份或数量重复，本次不判断偏离")
        notional = item.get("notional")
        try:
            parsed = Decimal(notional)
        except (InvalidOperation, TypeError) as exc:
            raise RebalanceError(503, "存在缺失或非法名义的持仓腿，本次不判断偏离") from exc
        if not parsed.is_finite() or parsed < 0:
            raise RebalanceError(503, "存在缺失或非法名义的持仓腿，本次不判断偏离")
        expected_side = "long" if coin == descriptor["coins"][0] else "short"
        if item.get("side") != expected_side:
            raise RebalanceError(503, "现有方向不符合 pair 多空约束，本次不生成候选")
        by_coin[coin] = parsed
    target = Decimal(rule["target_notional"])
    threshold = Decimal(rule["threshold_pct"])
    notionals = [by_coin.get(coin, Decimal("0")) for coin in descriptor["coins"]]
    drifts = [abs(value - target) / target * Decimal("100") for value in notionals]
    observed = [] if position_state == "flat" else [
        index for index, drift in enumerate(drifts) if drift >= threshold
    ]
    return {
        "pair_config_revision": revision,
        "current_notionals": [format(value, "f") for value in notionals],
        "drift_pct": [format(value.normalize(), "f") for value in drifts],
        "open_orders": open_orders,
        "position_state": position_state,
        "observed_leg_indices": observed,
        "checked_at": queried_at,
    }


def _fresh_snapshot(pair_id: str, account: str) -> dict:
    return pairs_service.get_pair_dashboard_for_account(pair_id, account)


def _apply_evaluation(url: str, rule: dict, evaluation: dict) -> str:
    now = datetime.now(timezone.utc)
    observed = evaluation["observed_leg_indices"]
    previous_observed = rule["observed_leg_indices"]
    count = rule["consecutive_count"] + 1 if observed and observed == previous_observed else 1 if observed else 0
    candidate = rule["candidate_leg_indices"]
    candidate_preview = rule["candidate_preview"]
    candidate_at = rule["candidate_at"]
    cooldown_until = rule["cooldown_until"]
    status = "monitoring"
    event_type = None
    event_detail = None
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            _scope_lock(cursor, rule)
            blocked = _risk_blocked(cursor, rule)
            if rule["status"] in UNRESOLVED_STATUSES:
                status = rule["status"]
                count = 0
            elif rule["status"] == "cooling" and cooldown_until is not None and cooldown_until > now:
                status = "cooling"
                count = 0
            else:
                cooldown_until = None
                if blocked:
                    count = 0
                    if candidate:
                        event_type = "candidate_cleared"
                        event_detail = "Pair 风险退出优先，再平衡候选已取消"
                    candidate = []
                    candidate_at = None
                elif not observed:
                    if candidate:
                        event_type = "candidate_cleared"
                        event_detail = "两腿均已回到阈值内"
                    candidate = []
                    candidate_at = None
                elif rule["status"] == "candidate" and candidate == observed:
                    status = "candidate"
                    count = max(2, count)
                elif count >= 2:
                    status = "candidate"
                    candidate = observed
                    candidate_preview = evaluation.get("candidate_preview")
                    if candidate_preview is None:
                        raise RebalanceError(503, "候选调仓预览缺失，本次不生成候选")
                    candidate_at = now
                    event_type = "candidate"
                else:
                    if candidate:
                        event_type = "candidate_cleared"
                        event_detail = "超阈值腿已变化，重新连续确认"
                    candidate = []
                    candidate_at = None
            if not candidate:
                candidate_preview = None
            cursor.execute(
                """
                UPDATE hyperliquid_rebalance_rules SET
                    pair_config_revision = %s, status = %s, last_attempt_at = now(),
                    last_checked_at = %s, consecutive_count = %s, current_notionals = %s,
                    drift_pct = %s, open_orders = %s, position_state = %s,
                    observed_leg_indices = %s, candidate_leg_indices = %s,
                    candidate_preview = %s, candidate_at = %s,
                    cooldown_until = %s, last_error = NULL, updated_at = now()
                WHERE id = %s AND revision = %s AND enabled = true
                RETURNING id
                """,
                (
                    evaluation["pair_config_revision"], status, evaluation["checked_at"], count,
                    Json(evaluation["current_notionals"]), Json(evaluation["drift_pct"]),
                    evaluation["open_orders"], evaluation["position_state"], Json(observed), Json(candidate),
                    Json(candidate_preview) if candidate_preview is not None else None,
                    candidate_at, cooldown_until, rule["id"], rule["revision"],
                ),
            )
            if cursor.fetchone() is None:
                return "rule_changed"
            if event_type is not None:
                _event(
                    cursor, rule["id"], event_type, evaluation, leg_indices=candidate or observed,
                    detail=event_detail,
                )
    return status


def _mark_error(url: str, rule: dict, error: Exception) -> str:
    detail = error.detail if isinstance(error, RebalanceError) else "监控读取暂不可用"
    status = rule["status"] if rule["candidate_leg_indices"] or rule["status"] in UNRESOLVED_STATUSES else "unavailable"
    with connect(url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE hyperliquid_rebalance_rules SET
                    status = %s, last_attempt_at = now(), consecutive_count = 0,
                    observed_leg_indices = '[]', last_error = %s, updated_at = now()
                WHERE id = %s AND revision = %s AND enabled = true
                """,
                (status, detail[:500], rule["id"], rule["revision"]),
            )
    return "unavailable"


def monitor_once(url: str, snapshot_loader=None, preview_loader=None) -> dict:
    loader = snapshot_loader or _fresh_snapshot
    preview = preview_loader or adjustments_service.preview
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM hyperliquid_rebalance_rules WHERE enabled = true ORDER BY id")
            rules = [dict(row) for row in cursor.fetchall()]
    results = []
    for rule in rules:
        try:
            evaluation = evaluate_snapshot(loader(rule["pair_id"], rule["account_address"]), rule)
            observed = evaluation["observed_leg_indices"]
            needs_preview = bool(
                observed and observed == rule["observed_leg_indices"] and rule["consecutive_count"] >= 1
                and rule["status"] not in UNRESOLVED_STATUSES | {"cooling"}
                and not (rule["status"] == "candidate" and rule["candidate_leg_indices"] == observed)
            )
            if needs_preview:
                evaluation["candidate_preview"] = preview(
                    url, rule["pair_id"], rule["account_address"], evaluation["pair_config_revision"],
                    _decimal_text(rule["target_notional"]), rule.get("owner_account_id"), observed,
                )
            status = _apply_evaluation(url, rule, evaluation)
        except Exception as exc:
            status = _mark_error(url, rule, exc)
        results.append({"rule_id": rule["id"], "pair_id": rule["pair_id"], "status": status})
    return {"kind": "pair_rebalance", "checked": len(results), "results": results}


def record_execution_result(
    url: str, pair_id: str, account_address: str, owner_account_id: int | None, report: dict,
) -> None:
    try:
        target = Decimal(str(report.get("target_notional")))
    except InvalidOperation:
        return
    if report.get("pair_id") != pair_id:
        return
    account = accounts_service.normalize_address(account_address)
    if report.get("account_address") != account:
        raise RebalanceError(503, "再平衡结果账户身份不一致")
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            row = _rule_row(cursor, pair_id, account, owner_account_id, lock=True)
            if row is None or not row["candidate_leg_indices"] or target != Decimal(row["target_notional"]):
                return
            if report.get("leg_indices", [0, 1]) != row["candidate_leg_indices"]:
                return
            _scope_lock(cursor, row)
            row = _rule_row(cursor, pair_id, account, owner_account_id, lock=True)
            if row is None or not row["candidate_leg_indices"]:
                return
            managed_status = report.get("managed_status")
            report_status = report.get("status")
            if managed_status == "running" or report_status == "executing":
                status = "executing"
            elif managed_status == "failed":
                status = "candidate"
            elif report_status == "succeeded":
                status = "cooling"
            elif report_status == "incomplete":
                status = "incomplete"
            elif report_status == "unknown":
                status = "unknown"
            else:
                return
            action_id = report.get("action_id")
            request_id = report.get("request_id")
            if (
                row["status"] == status and row["last_action_id"] == action_id
                and (row["last_request_id"] is None if request_id is None else str(row["last_request_id"]) == request_id)
            ):
                return
            cooldown = datetime.now(timezone.utc) + timedelta(seconds=_cooldown_seconds()) if status == "cooling" else None
            cursor.execute(
                """
                UPDATE hyperliquid_rebalance_rules SET
                    status = %s, cooldown_until = %s, last_action_id = %s,
                    last_request_id = %s, last_error = NULL, updated_at = now()
                WHERE id = %s
                """,
                (status, cooldown, action_id, request_id, row["id"]),
            )
            _event(
                cursor, row["id"], "execution_updated", leg_indices=row["candidate_leg_indices"],
                action_id=action_id, request_id=request_id, detail=status,
            )
