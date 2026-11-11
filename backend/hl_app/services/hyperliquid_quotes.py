from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from decimal import Decimal, InvalidOperation

from . import hyperliquid_pairs as pairs_service
from .hyperliquid_adjustments import AdjustmentError
from .process import HyperliquidUnavailable, communicate_bounded

QUOTE_TIMEOUT_SECONDS = 15
QUOTE_CACHE_TTL_SECONDS = 1.0
MAX_QUOTE_PROCESSES = 2
QUOTE_KEYS = {"schema_version", "pair_id", "network", "config_revision", "queried_at", "legs"}
LEG_KEYS = {"coin", "bid", "ask", "mid", "book_time"}

_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_locks: dict[tuple[str, str], threading.Lock] = {}
_locks_guard = threading.Lock()
_processes = threading.Semaphore(MAX_QUOTE_PROCESSES)


def clear_quote_cache() -> None:
    _cache.clear()


def _key_lock(key: tuple[str, str]) -> threading.Lock:
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


def _price(value, label: str) -> Decimal:
    if not isinstance(value, str) or not value.strip():
        raise AdjustmentError(503, f"盘口 {label} 不是十进制字符串")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise AdjustmentError(503, f"盘口 {label} 不是合法十进制数") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise AdjustmentError(503, f"盘口 {label} 必须为正有限数")
    return parsed


def _validate(payload, descriptor: dict, revision: str) -> dict:
    if not isinstance(payload, dict) or set(payload) != QUOTE_KEYS:
        raise AdjustmentError(503, "盘口响应字段不完整")
    if (
        type(payload.get("schema_version")) is not int
        or payload["schema_version"] != 1
        or payload.get("pair_id") != descriptor["id"]
        or payload.get("network") != descriptor["network"]
        or payload.get("config_revision") != revision
        or not isinstance(payload.get("queried_at"), str)
        or not payload["queried_at"].endswith("Z")
        or not isinstance(payload.get("legs"), list)
        or len(payload["legs"]) != 2
    ):
        raise AdjustmentError(503, "盘口响应身份校验失败")
    for index, leg in enumerate(payload["legs"]):
        if not isinstance(leg, dict) or set(leg) != LEG_KEYS:
            raise AdjustmentError(503, "盘口腿字段不完整")
        if leg.get("coin") != descriptor["coins"][index] or not isinstance(leg.get("book_time"), str) or not leg["book_time"].endswith("Z"):
            raise AdjustmentError(503, "盘口腿身份或时间校验失败")
        bid = _price(leg.get("bid"), "bid")
        ask = _price(leg.get("ask"), "ask")
        mid = _price(leg.get("mid"), "mid")
        if ask < bid or mid != (bid + ask) / 2:
            raise AdjustmentError(503, "盘口买卖价或中间价不一致")
    return payload


def _collect(descriptor: dict, revision: str) -> dict:
    binary = os.environ.get("HL_APP_TRADER_BIN", "").strip()
    config = os.environ.get("HL_APP_PAIRS_CONFIG", "").strip()
    if not binary or not config:
        raise AdjustmentError(503, "盘口执行器未配置")
    argv = [binary, "--config", config, "adjust-quote", "--pair", descriptor["id"], "--json"]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise AdjustmentError(503, "无法启动盘口执行器") from exc
    try:
        stdout, stderr = communicate_bounded(proc, QUOTE_TIMEOUT_SECONDS)
    except HyperliquidUnavailable as exc:
        raise AdjustmentError(503, "盘口读取超时或中断") from exc
    if proc.returncode != 0:
        raise AdjustmentError(503, f"盘口读取失败: {stderr.strip()[:200]}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AdjustmentError(503, "盘口执行器返回非法 JSON") from exc
    payload = _validate(payload, descriptor, revision)
    if pairs_service.load_pairs()[0] != revision:
        raise AdjustmentError(409, "pair 配置已变化，请重新读取盘口")
    return payload


def get_adjustment_quote(pair_id: str, config_revision: str) -> dict:
    revision, descriptors = pairs_service.load_pairs()
    if revision != config_revision:
        raise AdjustmentError(409, "pair 配置已变化，请刷新页面")
    descriptor = next((item for item in descriptors if item["id"] == pair_id), None)
    if descriptor is None:
        raise AdjustmentError(404, f"未知或停用的 pair: {pair_id}")
    key = (pair_id, revision)
    lock = _key_lock(key)
    with lock:
        hit = _cache.get(key)
        now = time.monotonic()
        if hit is not None and now - hit[0] < QUOTE_CACHE_TTL_SECONDS:
            return hit[1]
        with _processes:
            hit = _cache.get(key)
            now = time.monotonic()
            if hit is not None and now - hit[0] < QUOTE_CACHE_TTL_SECONDS:
                return hit[1]
            payload = _collect(descriptor, revision)
            _cache[key] = (time.monotonic(), payload)
            return payload
