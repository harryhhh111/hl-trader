from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from psycopg2.extras import RealDictCursor

from ..db import connect
from . import hyperliquid_accounts as accounts_service
from . import hyperliquid_adjustments as adjustments_service
from . import hyperliquid_pairs as pairs_service

STATUSES = {"disabled", "monitoring", "triggered", "executing", "completed", "incomplete", "unknown", "unavailable"}
UNRESOLVED_STATUSES = {"executing", "incomplete", "unknown"}


class RiskError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _decimal(value: str | None, label: str) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise RiskError(422, f"{label}必须是十进制数") from exc
    if not parsed.is_finite() or parsed <= 0 or parsed > Decimal("1000000000"):
        raise RiskError(422, f"{label}必须是大于 0 且不超过 1000000000 的有限金额")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _stale_seconds() -> int:
    try:
        value = int(os.environ.get("HL_APP_RISK_MONITOR_STALE_SECONDS", "60"))
    except ValueError as exc:
        raise RiskError(503, "HL_APP_RISK_MONITOR_STALE_SECONDS 配置非法") from exc
    if not 15 <= value <= 3600:
        raise RiskError(503, "HL_APP_RISK_MONITOR_STALE_SECONDS 必须在 15 到 3600 之间")
    return value


def _max_data_age_seconds() -> int:
    try:
        value = int(os.environ.get("HL_APP_RISK_MAX_DATA_AGE_SECONDS", "30"))
    except ValueError as exc:
        raise RiskError(503, "HL_APP_RISK_MAX_DATA_AGE_SECONDS 配置非法") from exc
    if not 5 <= value <= 300:
        raise RiskError(503, "HL_APP_RISK_MAX_DATA_AGE_SECONDS 必须在 5 到 300 之间")
    return value


def _scope_lock(cursor, rule: dict) -> None:
    key = f"risk:{rule.get('owner_account_id')}:{rule['network']}:{rule['pair_id']}:{rule['account_address']}"
    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))


def _rule_row(cursor, pair_id: str, account: str, owner_account_id: int | None, lock: bool = False) -> dict | None:
    cursor.execute(
        f"""
        SELECT * FROM hyperliquid_pair_risk_rules
        WHERE pair_id = %s AND account_address = %s AND owner_account_id IS NOT DISTINCT FROM %s
        {'FOR UPDATE' if lock else ''}
        """,
        (pair_id, account, owner_account_id),
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _event(cursor, rule_id: int, event_type: str, **values) -> None:
    cursor.execute(
        """
        INSERT INTO hyperliquid_pair_risk_events (
            rule_id, event_type, trigger_kind, pair_unrealized_pnl, action_id, request_id, detail
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            rule_id, event_type, values.get("trigger_kind"), values.get("pair_unrealized_pnl"),
            values.get("action_id"), values.get("request_id"), values.get("detail"),
        ),
    )


def _view(row: dict | None, pair_id: str, account: str, events: list[dict] | None = None) -> dict:
    stale_seconds = _stale_seconds()
    if row is None:
        return {
            "configured": False,
            "pair_id": pair_id,
            "account_address": account,
            "enabled": False,
            "take_profit_usd": None,
            "stop_loss_usd": None,
            "rule_revision": 0,
            "status": "disabled",
            "worker_available": False,
            "last_attempt_at": None,
            "last_checked_at": None,
            "pair_unrealized_pnl": None,
            "position_state": None,
            "trigger_kind": None,
            "triggered_at": None,
            "trigger_pnl": None,
            "last_action_id": None,
            "last_request_id": None,
            "last_error": None,
            "events": [],
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
        "take_profit_usd": _decimal_text(row["take_profit_usd"]),
        "stop_loss_usd": _decimal_text(row["stop_loss_usd"]),
        "rule_revision": row["revision"],
        "status": row["status"],
        "worker_available": worker_available,
        "last_attempt_at": _timestamp(last_attempt),
        "last_checked_at": _timestamp(row["last_checked_at"]),
        "pair_unrealized_pnl": _decimal_text(row["pair_unrealized_pnl"]),
        "position_state": row["position_state"],
        "trigger_kind": row["trigger_kind"],
        "triggered_at": _timestamp(row["triggered_at"]),
        "trigger_pnl": _decimal_text(row["trigger_pnl"]),
        "last_action_id": row["last_action_id"],
        "last_request_id": None if row["last_request_id"] is None else str(row["last_request_id"]),
        "last_error": row["last_error"],
        "updated_at": _timestamp(row["updated_at"]),
        "events": [
            {
                "id": event["id"],
                "event_type": event["event_type"],
                "trigger_kind": event["trigger_kind"],
                "pair_unrealized_pnl": _decimal_text(event["pair_unrealized_pnl"]),
                "action_id": event["action_id"],
                "request_id": None if event["request_id"] is None else str(event["request_id"]),
                "detail": event["detail"],
                "created_at": _timestamp(event["created_at"]),
            }
            for event in (events or [])
        ],
    }


def save_rule(
    url: str, pair_id: str, account_address: str, config_revision: str, enabled: bool,
    take_profit_usd: str | None, stop_loss_usd: str | None, owner_account_id: int | None = None,
) -> dict:
    if type(enabled) is not bool:
        raise RiskError(422, "enabled 必须是布尔值")
    take_profit = _decimal(take_profit_usd, "止盈金额")
    stop_loss = _decimal(stop_loss_usd, "止损金额")
    if enabled and take_profit is None and stop_loss is None:
        raise RiskError(422, "启用监控前至少设置止盈或止损金额")
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
                enabled, take_profit, stop_loss, status,
            )
            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO hyperliquid_pair_risk_rules (
                        owner_account_id, network, pair_id, account_address, pair_config_revision,
                        enabled, take_profit_usd, stop_loss_usd, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
                    """,
                    values,
                )
            else:
                cursor.execute(
                    """
                    UPDATE hyperliquid_pair_risk_rules SET
                        pair_config_revision = %s, enabled = %s, take_profit_usd = %s,
                        stop_loss_usd = %s, revision = %s, status = %s,
                        last_attempt_at = NULL, last_checked_at = NULL, pair_unrealized_pnl = NULL,
                        position_state = NULL, trigger_kind = NULL, triggered_at = NULL,
                        trigger_pnl = NULL, last_action_id = NULL, last_request_id = NULL,
                        last_error = NULL, updated_at = now()
                    WHERE id = %s RETURNING *
                    """,
                    (config_revision, enabled, take_profit, stop_loss, revision, status, existing["id"]),
                )
            row = dict(cursor.fetchone())
            _event(cursor, row["id"], "rule_updated", detail=f"revision={revision}; enabled={enabled}")
            return _view(row, pair_id, account)


def get_rule(url: str, pair_id: str, account_address: str, owner_account_id: int | None = None) -> dict:
    account = accounts_service.normalize_address(account_address)
    revision, descriptors = pairs_service.load_pairs()
    descriptor = next((item for item in descriptors if item["id"] == pair_id), None)
    if descriptor is None:
        raise RiskError(404, "未知或停用的 pair")
    active = accounts_service.apply_active_accounts(url, [descriptor], owner_account_id)[0]["account_address"]
    if active != account:
        raise RiskError(409, "当前账户已变化，请刷新页面")
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            row = _rule_row(cursor, pair_id, account, owner_account_id)
            events = []
            if row is not None:
                cursor.execute(
                    "SELECT * FROM hyperliquid_pair_risk_events WHERE rule_id = %s ORDER BY id DESC LIMIT 10",
                    (row["id"],),
                )
                events = [dict(item) for item in cursor.fetchall()]
            return _view(row, pair_id, account, events)


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
        raise RiskError(409, "pair 已停用或配置已删除")
    pair = snapshot.get("pair") or {}
    if (
        snapshot.get("schema_version") != 2 or snapshot.get("config_revision") != revision
        or pair.get("id") != rule["pair_id"] or pair.get("network") != rule["network"]
        or pair.get("dex") != descriptor["dex"] or pair.get("coins") != descriptor["coins"]
        or str(pair.get("account_address") or "").lower() != rule["account_address"]
    ):
        raise RiskError(503, "监控快照身份或配置版本不一致")
    queried_at = _parse_time(snapshot["queried_at"])
    age = (now - queried_at).total_seconds()
    if age < -5 or age > _max_data_age_seconds():
        raise RiskError(503, "监控快照已过期或时间异常")
    positions = (snapshot.get("blocks") or {}).get("positions") or {}
    items = positions.get("items")
    if positions.get("status") != "ok" or not isinstance(items, list) or positions.get("unrealized_pnl") is None:
        raise RiskError(503, "两腿持仓或盈亏数据不完整，本次不判断触发")
    pnl = Decimal("0")
    for item in items:
        if item.get("unrealized_pnl") is None:
            raise RiskError(503, "存在缺失盈亏的持仓腿，本次不判断触发")
        pnl += Decimal(item["unrealized_pnl"])
    if pnl != Decimal(positions["unrealized_pnl"]):
        raise RiskError(503, "两腿盈亏合计校验不一致")
    position_state = positions.get("position_state")
    if position_state not in {"flat", "single_leg", "opposite", "same_direction"}:
        raise RiskError(503, "持仓状态未知，本次不判断触发")
    trigger_kind = None
    if rule["take_profit_usd"] is not None and pnl >= rule["take_profit_usd"]:
        trigger_kind = "take_profit"
    elif rule["stop_loss_usd"] is not None and pnl <= -rule["stop_loss_usd"]:
        trigger_kind = "stop_loss"
    return {
        "pair_config_revision": revision,
        "pair_unrealized_pnl": pnl,
        "position_state": position_state,
        "trigger_kind": trigger_kind,
        "checked_at": queried_at,
    }


def _fresh_snapshot(pair_id: str, account: str) -> dict:
    return pairs_service.get_pair_dashboard_for_account(pair_id, account)


def _apply_evaluation(url: str, rule: dict, evaluation: dict) -> str:
    now = datetime.now(timezone.utc)
    trigger = evaluation["trigger_kind"]
    previous_trigger = rule["trigger_kind"]
    flat = evaluation["position_state"] == "flat"
    status = "triggered" if trigger else "monitoring"
    event_type = None
    if flat and previous_trigger is not None:
        status = "completed"
        event_type = "completed" if rule["status"] != "completed" else None
    elif rule["status"] in UNRESOLVED_STATUSES:
        status = rule["status"]
    elif trigger:
        if trigger != previous_trigger or rule["status"] != "triggered":
            event_type = "triggered"
    elif previous_trigger is not None:
        event_type = "trigger_cleared"
    if status == "monitoring" and rule["status"] not in UNRESOLVED_STATUSES:
        previous_trigger = None
    triggered_at = now if event_type == "triggered" else rule["triggered_at"]
    trigger_pnl = evaluation["pair_unrealized_pnl"] if event_type == "triggered" else rule["trigger_pnl"]
    stored_trigger = trigger if trigger is not None else previous_trigger
    if status == "monitoring":
        stored_trigger = None
        triggered_at = None
        trigger_pnl = None
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            _scope_lock(cursor, rule)
            cursor.execute(
                """
                UPDATE hyperliquid_pair_risk_rules SET
                    pair_config_revision = %s, status = %s, last_attempt_at = now(),
                    last_checked_at = %s, pair_unrealized_pnl = %s, position_state = %s,
                    trigger_kind = %s, triggered_at = %s, trigger_pnl = %s,
                    last_error = NULL, updated_at = now()
                WHERE id = %s AND revision = %s AND enabled = true
                RETURNING id
                """,
                (
                    evaluation["pair_config_revision"], status, evaluation["checked_at"],
                    evaluation["pair_unrealized_pnl"], evaluation["position_state"], stored_trigger,
                    triggered_at, trigger_pnl, rule["id"], rule["revision"],
                ),
            )
            updated = cursor.fetchone()
            if updated is None:
                return "rule_changed"
            if event_type is not None:
                _event(
                    cursor, rule["id"], event_type, trigger_kind=stored_trigger,
                    pair_unrealized_pnl=evaluation["pair_unrealized_pnl"],
                )
    return status


def _mark_error(url: str, rule: dict, error: Exception) -> str:
    detail = error.detail if isinstance(error, RiskError) else "监控读取暂不可用"
    detail = detail[:500]
    status = rule["status"] if rule["trigger_kind"] is not None or rule["status"] in UNRESOLVED_STATUSES else "unavailable"
    with connect(url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE hyperliquid_pair_risk_rules SET
                    status = %s, last_attempt_at = now(), last_error = %s, updated_at = now()
                WHERE id = %s AND revision = %s AND enabled = true
                """,
                (status, detail, rule["id"], rule["revision"]),
            )
    return "unavailable"


def monitor_once(url: str, snapshot_loader=None) -> dict:
    loader = snapshot_loader or _fresh_snapshot
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM hyperliquid_pair_risk_rules WHERE enabled = true ORDER BY id")
            rules = [dict(row) for row in cursor.fetchall()]
    results = []
    for rule in rules:
        try:
            evaluation = evaluate_snapshot(loader(rule["pair_id"], rule["account_address"]), rule)
            status = _apply_evaluation(url, rule, evaluation)
        except Exception as exc:
            status = _mark_error(url, rule, exc)
        results.append({"rule_id": rule["id"], "pair_id": rule["pair_id"], "status": status})
    return {"kind": "pair_risk", "checked": len(results), "results": results}


def record_close_result(
    url: str, pair_id: str, account_address: str, owner_account_id: int | None, report: dict,
) -> None:
    try:
        target = Decimal(str(report.get("target_notional")))
    except InvalidOperation:
        return
    if target != 0 or report.get("pair_id") != pair_id:
        return
    account = accounts_service.normalize_address(account_address)
    if report.get("account_address") != account:
        raise RiskError(503, "平仓结果账户身份不一致")
    managed_status = report.get("managed_status")
    report_status = report.get("status")
    if managed_status == "running" or report_status == "executing":
        status = "executing"
    elif managed_status == "failed":
        status = "triggered"
    elif report_status == "succeeded":
        status = "completed"
    elif report_status == "incomplete":
        status = "incomplete"
    elif report_status == "unknown":
        status = "unknown"
    else:
        return
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            row = _rule_row(cursor, pair_id, account, owner_account_id, lock=True)
            if row is None or row["trigger_kind"] is None:
                return
            _scope_lock(cursor, row)
            row = _rule_row(cursor, pair_id, account, owner_account_id, lock=True)
            if row is None or row["trigger_kind"] is None:
                return
            action_id = report.get("action_id")
            request_id = report.get("request_id")
            if (
                row["status"] == status and row["last_action_id"] == action_id
                and (row["last_request_id"] is None if request_id is None else str(row["last_request_id"]) == request_id)
            ):
                return
            cursor.execute(
                """
                UPDATE hyperliquid_pair_risk_rules SET
                    status = %s, last_action_id = %s, last_request_id = %s,
                    last_error = NULL, updated_at = now()
                WHERE id = %s
                """,
                (status, action_id, request_id, row["id"]),
            )
            _event(
                cursor, row["id"], "execution_updated", trigger_kind=row["trigger_kind"],
                pair_unrealized_pnl=row["pair_unrealized_pnl"], action_id=action_id,
                request_id=request_id, detail=status,
            )
