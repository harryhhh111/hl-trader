"""Account-bound, read-only margin checks; execution is rechecked by Rust."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone

from . import hyperliquid_adjustments as adjustments
from .process import HyperliquidUnavailable, communicate_bounded

_processes = threading.BoundedSemaphore(2)
MAX_LEVERAGE = 3


def _validate(payload, descriptor: dict, account: str, revision: str) -> dict:
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
        or payload.get("pair_id") != descriptor["id"]
        or payload.get("network") != descriptor["network"]
        or payload.get("account_address") != account
        or payload.get("config_revision") != revision
        or type(payload.get("policy_max_leverage")) is not int or payload["policy_max_leverage"] != MAX_LEVERAGE
        or type(payload.get("ready_for_increase")) is not bool
        or not isinstance(payload.get("legs"), list) or len(payload["legs"]) != 2
    ):
        raise adjustments.AdjustmentError(503, "下单前检查身份或规则校验失败")
    try:
        timestamp = datetime.fromisoformat(payload["queried_at"].replace("Z", "+00:00"))
        if timestamp.tzinfo is None or not -5 <= (datetime.now(timezone.utc) - timestamp).total_seconds() <= 30:
            raise ValueError("stale")
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise adjustments.AdjustmentError(503, "下单前检查时间无效或已过期") from exc
    for coin, leg in zip(descriptor["coins"], payload["legs"]):
        if (
            not isinstance(leg, dict) or leg.get("coin") != coin
            or leg.get("margin_mode") not in ("cross", "isolated")
            or type(leg.get("leverage")) is not int or leg["leverage"] < 1
            or type(leg.get("market_max_leverage")) is not int or leg["market_max_leverage"] < 1
            or type(leg.get("cross_available")) is not bool
            or type(leg.get("ready_for_increase")) is not bool
            or not isinstance(leg.get("diagnostics"), list)
            or any(not isinstance(message, str) for message in leg["diagnostics"])
        ):
            raise adjustments.AdjustmentError(503, "保证金或杠杆检查数据非法")
        expected = (
            leg["leverage"] <= min(MAX_LEVERAGE, leg["market_max_leverage"])
            and leg["margin_mode"] == ("cross" if leg["cross_available"] else "isolated")
        )
        # A delisted market may conservatively return false even with otherwise safe settings.
        if leg["ready_for_increase"] and not expected:
            raise adjustments.AdjustmentError(503, "下单前检查结果与保证金规则不一致")
    if payload["ready_for_increase"] != all(leg["ready_for_increase"] for leg in payload["legs"]):
        raise adjustments.AdjustmentError(503, "下单前检查两腿状态不一致")
    return payload


def get_preflight(
    url: str, pair_id: str, account_address: str, config_revision: str,
    owner_account_id: int | None = None,
) -> dict:
    descriptor, account = adjustments._resolve(url, pair_id, account_address, config_revision, owner_account_id)
    binary = os.environ.get("HL_APP_TRADER_BIN", "").strip()
    config = os.environ.get("HL_APP_PAIRS_CONFIG", "").strip()
    if not binary or not config:
        raise adjustments.AdjustmentError(503, "下单前检查执行器未配置")
    argv = [binary, "--config", config, "adjust-preflight", "--pair", pair_id, "--address", account, "--json"]
    with _processes:
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, _stderr = communicate_bounded(proc, 30)
        except (OSError, HyperliquidUnavailable) as exc:
            raise adjustments.AdjustmentError(503, "下单前检查超时或中断，禁止增仓") from exc
        if proc.returncode != 0:
            raise adjustments.AdjustmentError(503, "无法读取最新保证金或杠杆，禁止增仓")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise adjustments.AdjustmentError(503, "下单前检查返回非法 JSON") from exc
        result = _validate(payload, descriptor, account, config_revision)
    adjustments._resolve(url, pair_id, account_address, config_revision, owner_account_id)
    return result
