from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import hyperliquid_accounts as accounts_service
from . import hyperliquid_adjustments as adjustments

ACTION_ID_RE = re.compile(r"^adjust-0x[0-9a-f]{32}$")
ARCHIVE_RE = re.compile(
    r"^adjust-adjust-0x[0-9a-f]{32}-(?:succeeded|incomplete|unknown)\.json$"
)
REVISION_RE = re.compile(r"^[0-9a-f]{64}$")
REPORT_STATUSES = {"executing", "succeeded", "incomplete", "unknown"}
LEG_STATUSES = {
    "not_required", "prepared", "not_sent", "pending", "filled", "partially_filled", "rejected", "unknown",
}
MAX_REPORT_BYTES = 1024 * 1024
DEFAULT_LIMIT = 50


class TradeHistoryError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _decimal(value) -> str:
    if not isinstance(value, str):
        raise ValueError("decimal must be a string")
    parsed = adjustments._decimal(value, "交易历史数值")
    if parsed < 0:
        raise ValueError("decimal must not be negative")
    return value


def _signed_decimal(value) -> str | None:
    if value is None:
        return None
    adjustments._decimal(value, "滑点数值")
    return value


def _timestamp(value) -> str:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return value


def _leg(value: dict, coins: list[str]) -> dict:
    if not isinstance(value, dict) or value.get("coin") not in coins:
        raise ValueError("invalid leg identity")
    if value.get("position_side") not in {"long", "short"}:
        raise ValueError("invalid position side")
    if value.get("action") not in {"buy", "sell", "none"}:
        raise ValueError("invalid action")
    if value.get("status") not in LEG_STATUSES or type(value.get("reduce_only")) is not bool:
        raise ValueError("invalid leg status")
    oid = value.get("oid")
    if oid is not None and (type(oid) is not int or oid < 0):
        raise ValueError("invalid order id")
    result = value.get("result")
    if result is not None and not isinstance(result, str):
        raise ValueError("invalid leg result")
    avg_px = value.get("avg_px")
    return {
        "coin": value["coin"],
        "position_side": value["position_side"],
        "action": value["action"],
        "reduce_only": value["reduce_only"],
        "order_qty": _decimal(value.get("order_qty")),
        "protect_px": _decimal(value.get("protect_px")),
        "status": value["status"],
        "confirmed_qty": _decimal(value.get("confirmed_qty")),
        "avg_px": None if avg_px is None else _decimal(avg_px),
        "slippage_pct": _signed_decimal(value.get("slippage_pct")),
        "slippage_usd": _signed_decimal(value.get("slippage_usd")),
        "oid": oid,
        "cloid": value.get("cloid") if isinstance(value.get("cloid"), str) else None,
        "result": result,
    }


def _report(value: dict, descriptor: dict, account: str) -> dict | None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("invalid report schema")
    if value.get("status") in {"preview", "prepared"}:
        return None
    if value.get("status") not in REPORT_STATUSES:
        raise ValueError("invalid report status")
    if (
        value.get("pair_id") != descriptor["id"]
        or value.get("network") != descriptor["network"]
        or value.get("account_address") != account
        or not isinstance(value.get("config_revision"), str)
        or not REVISION_RE.fullmatch(value["config_revision"])
        or not isinstance(value.get("action_id"), str)
        or not ACTION_ID_RE.fullmatch(value["action_id"])
    ):
        raise ValueError("report identity mismatch")
    legs = value.get("legs")
    if (
        not isinstance(legs, list) or len(legs) != 2
        or {leg.get("coin") for leg in legs if isinstance(leg, dict)} != set(descriptor["coins"])
    ):
        raise ValueError("invalid report legs")
    diagnostics = value.get("diagnostics")
    if not isinstance(diagnostics, list) or not all(isinstance(item, str) for item in diagnostics):
        raise ValueError("invalid diagnostics")
    request_id = value.get("request_id")
    if request_id is not None and not isinstance(request_id, str):
        raise ValueError("invalid request id")
    return {
        "action_id": value["action_id"],
        "request_id": request_id,
        "execution_mode": "managed" if request_id else "browser" if value.get("expected_signer") else "server",
        "status": "running" if value["status"] == "executing" else value["status"],
        "target_notional": _decimal(value.get("target_notional")),
        "created_at": _timestamp(value.get("created_at")),
        "legs": [_leg(leg, descriptor["coins"]) for leg in legs],
        "diagnostics": diagnostics,
    }


def read_history(
    state_dir: str, descriptor: dict, account: str, limit: int = DEFAULT_LIMIT,
) -> dict:
    directory = Path(state_dir) / descriptor["id"] / account
    try:
        current = directory / "adjust-state.json"
        paths = [current] if current.is_file() else []
        archives = [path for path in directory.iterdir() if path.is_file() and ARCHIVE_RE.fullmatch(path.name)]
        paths.extend(sorted(archives, key=lambda path: path.stat().st_mtime_ns, reverse=True))
    except FileNotFoundError:
        paths = []
    except OSError as exc:
        raise TradeHistoryError(503, "交易历史暂不可用") from exc

    items = []
    seen_actions = set()
    skipped = 0
    for path in paths:
        try:
            if path.stat().st_size > MAX_REPORT_BYTES:
                raise ValueError("report is too large")
            value = json.loads(path.read_text(encoding="utf-8"))
            item = _report(value, descriptor, account)
            if item is None or item["action_id"] in seen_actions:
                continue
            seen_actions.add(item["action_id"])
            items.append(item)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError, adjustments.AdjustmentError):
            skipped += 1
    items.sort(key=lambda item: item["created_at"], reverse=True)
    return {
        "pair_id": descriptor["id"],
        "account_address": account,
        "items": items[:limit],
        "has_more": len(items) > limit,
        "skipped_reports": skipped,
    }


def list_history(url: str, descriptor: dict, account_address: str, owner_account_id: int | None = None) -> dict:
    account = accounts_service.normalize_address(account_address)
    active = accounts_service.apply_active_accounts(url, [descriptor], owner_account_id)[0]["account_address"]
    if active != account:
        raise accounts_service.PairAccountError(409, "当前账户已变化，请刷新页面")
    return read_history(adjustments._state_dir(), descriptor, account)
