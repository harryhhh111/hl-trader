"""v2 多 pair 看板：服务端 JSON 配置校验、目录、快照采集与隔离缓存。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .process import (
    PROC_TIMEOUT_SECONDS,
    HyperliquidUnavailable,
    communicate_bounded,
)

CONFIG_VERSION = 1
SCHEMA_VERSION = 2
MAX_ID_LEN = 64
PAIR_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
ZERO_ADDRESS = "0x" + "0" * 40
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TOP_KEYS = {"config_version", "pairs"}
PAIR_KEYS = {"id", "label", "enabled", "network", "dex", "coins",
             "account_address", "unlock_anchors"}
NETWORKS = ("mainnet", "testnet")
ANCHOR_KEYS = {"date_utc", "label", "source_url", "verified_utc"}

_cache: dict = {"revision": None, "pairs": None}
_lock = threading.Lock()


class PairsConfigError(RuntimeError):
    """配置不可用：未配置环境变量、文件不可读或校验失败。

    message 供服务端日志；public（若非 None）为可对外展示的安全简短原因。
    """

    def __init__(self, message: str, public: str | None = None):
        super().__init__(message)
        self.public = public


def _reject_dup_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"重复 JSON 键: {key}")
        result[key] = value
    return result


def _fail(message: str) -> None:
    raise PairsConfigError(message)


def _require_str(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{what} 必须是非空字符串")
    return value


def _validate_anchor(item) -> None:
    if not isinstance(item, dict):
        _fail("unlock_anchors 条目必须是对象")
    if set(item) != ANCHOR_KEYS:
        _fail(f"unlock_anchors 条目字段必须为 {sorted(ANCHOR_KEYS)}")
    for key in ANCHOR_KEYS:
        _require_str(item[key], f"unlock_anchors.{key}")
    for key in ("date_utc", "verified_utc"):
        if not DATE_RE.fullmatch(item[key]):
            _fail(f"unlock_anchors.{key} 必须是 YYYY-MM-DD")


def _validate_pair(entry, seen_ids: set) -> tuple[dict, bool]:
    if not isinstance(entry, dict):
        _fail("pairs 条目必须是对象")
    keys = set(entry)
    if keys - PAIR_KEYS:
        _fail(f"pair 含未知字段: {sorted(keys - PAIR_KEYS)}")
    if PAIR_KEYS - keys:
        _fail(f"pair 缺少字段: {sorted(PAIR_KEYS - keys)}")

    pid = entry["id"]
    if not isinstance(pid, str) or len(pid) > MAX_ID_LEN or not PAIR_ID_RE.fullmatch(pid):
        _fail(f"pair id 非法: {pid!r}")
    if pid in seen_ids:
        _fail(f"重复 pair id: {pid}")
    seen_ids.add(pid)

    label = _require_str(entry["label"], "label")
    if type(entry["enabled"]) is not bool:
        _fail("enabled 必须是布尔值")
    network = entry["network"]
    if network not in NETWORKS:
        _fail(f"network 只接受 mainnet|testnet: {network!r}")
    dex = entry["dex"]
    if not isinstance(dex, str):
        _fail("dex 必须是字符串")

    coins = entry["coins"]
    if (not isinstance(coins, list) or len(coins) != 2
            or not all(isinstance(c, str) and c for c in coins)):
        _fail("coins 必须是恰好两个非空字符串")
    if coins[0] == coins[1]:
        _fail("coins 两条腿必须不同")
    for coin in coins:
        if dex:
            prefix = f"{dex}:"
            if not coin.startswith(prefix) or ":" in coin[len(prefix):]:
                _fail(f"coin {coin!r} 必须是 {prefix}<name> 形式")
        elif ":" in coin:
            _fail(f"原生 DEX 的 coin 不能含冒号: {coin!r}")

    address = entry["account_address"]
    if address is not None:
        if not isinstance(address, str) or not ADDRESS_RE.fullmatch(address):
            _fail(f"account_address 非法: {address!r}")
        address = address.lower()
        if address == ZERO_ADDRESS:
            _fail("account_address 不允许零地址")

    anchors = entry["unlock_anchors"]
    if anchors is not None:
        if not isinstance(anchors, list):
            _fail("unlock_anchors 必须是数组或 null")
        for item in anchors:
            _validate_anchor(item)

    descriptor = {
        "id": pid,
        "label": label,
        "network": network,
        "dex": dex,
        "coins": list(coins),
        "account_address": address,
        "unlocks_enabled": anchors is not None,
    }
    return descriptor, entry["enabled"]


def _validate(raw: bytes) -> list[tuple[dict, bool]]:
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_dup_keys)
    except (UnicodeDecodeError, ValueError) as exc:
        _fail(f"pair 配置不是合法 JSON: {exc}")
    if not isinstance(data, dict):
        _fail("pair 配置顶层必须是对象")
    keys = set(data)
    if keys - TOP_KEYS:
        _fail(f"顶层含未知字段: {sorted(keys - TOP_KEYS)}")
    if TOP_KEYS - keys:
        _fail(f"顶层缺少字段: {sorted(TOP_KEYS - keys)}")
    if data["config_version"] != CONFIG_VERSION:
        _fail(f"config_version 不支持: {data['config_version']!r}")
    if not isinstance(data["pairs"], list):
        _fail("pairs 必须是数组")
    seen_ids: set = set()
    return [_validate_pair(entry, seen_ids) for entry in data["pairs"]]


def _read_config() -> tuple[bytes, str]:
    path = os.environ.get("HL_APP_PAIRS_CONFIG", "").strip()
    if not path:
        message = "未配置 HL_APP_PAIRS_CONFIG"
        raise PairsConfigError(message, public=message)
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise PairsConfigError(f"读取 pair 配置失败: {exc}") from exc
    return raw, hashlib.sha256(raw).hexdigest()


def load_pairs() -> tuple[str, list[dict]]:
    """返回 (config_revision, 启用 pair 目录)。配置按内容 SHA-256 缓存。"""
    raw, revision = _read_config()
    with _lock:
        if _cache["revision"] == revision:
            return revision, _cache["pairs"]
        validated = _validate(raw)
        pairs = [descriptor for descriptor, enabled in validated if enabled]
        _cache["revision"] = revision
        _cache["pairs"] = pairs
        return revision, pairs


def clear_cache() -> None:
    with _lock:
        _cache["revision"] = None
        _cache["pairs"] = None


# ─── v2 快照采集（Stage B） ───────────────────────────────

SNAPSHOT_CACHE_TTL_SECONDS = 30
MAX_COLLECTOR_PROCESSES = 2
BLOCK_NAMES = ("account", "positions", "funding", "ratio", "unlocks")

STATUS_VALUES = ("ok", "partial", "unavailable")
POSITION_STATES = ("flat", "single_leg", "opposite", "same_direction", "unknown")
SIDES = ("long", "short")
MARGIN_MODES = ("cross", "isolated")
EQUITY_SCOPES = ("perp_dex", "account", "unavailable")
DIRECTIONS = ("receive", "pay", "flat")

_collector_sem = threading.Semaphore(MAX_COLLECTOR_PROCESSES)
_key_locks: dict = {}
_key_locks_guard = threading.Lock()
_snapshot_cache: dict = {}
_announcements_cache: dict = {}


def _decimal_string(value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        number = Decimal(value)
    except InvalidOperation:
        return False
    return number.is_finite()


def _non_negative_decimal(value) -> bool:
    return _decimal_string(value) and Decimal(value) >= 0


def _timestamp(value) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _date_string(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _key_lock(key: str) -> threading.Lock:
    with _key_locks_guard:
        if key not in _key_locks:
            _key_locks[key] = threading.Lock()
        return _key_locks[key]


def clear_runtime_cache() -> None:
    _snapshot_cache.clear()
    _announcements_cache.clear()


_processes: set[subprocess.Popen] = set()
_processes_guard = threading.Lock()


def terminate_collectors() -> None:
    with _processes_guard:
        processes = list(_processes)
    for proc in processes:
        try:
            proc.kill()
        except OSError:
            pass


def _run_dashboard(collect_argv: list[str]) -> dict:
    """执行 hl-trader dashboard 子命令并返回解析后的 JSON。调用前后核对配置修订号。"""
    bin_path = os.environ.get("HL_APP_TRADER_BIN", "").strip()
    config_path = os.environ.get("HL_APP_PAIRS_CONFIG", "").strip()
    if not bin_path:
        raise HyperliquidUnavailable(
            "未配置 HL_APP_TRADER_BIN", public="未配置 HL_APP_TRADER_BIN",
        )
    _raw, revision_before = _read_config()
    argv = [bin_path, "--config", config_path, "dashboard", *collect_argv, "--json"]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        raise HyperliquidUnavailable(f"无法执行 hl-trader: argv={argv!r}")
    with _processes_guard:
        _processes.add(proc)
    try:
        stdout, _stderr = communicate_bounded(proc, PROC_TIMEOUT_SECONDS)
    finally:
        with _processes_guard:
            _processes.discard(proc)
    if proc.returncode != 0:
        raise HyperliquidUnavailable(f"hl-trader 退出码 {proc.returncode}: {_stderr.strip()[:200]}")
    try:
        snapshot = json.loads(stdout)
    except json.JSONDecodeError:
        raise HyperliquidUnavailable("hl-trader 输出不是合法 JSON", public="hl-trader 快照不是合法 JSON")
    if not isinstance(snapshot, dict):
        raise HyperliquidUnavailable("hl-trader 输出不是 JSON 对象", public="hl-trader 快照不是合法 JSON")
    _raw_after, revision_after = _read_config()
    if revision_after != revision_before:
        raise HyperliquidUnavailable(
            "pair 配置在调用中途被替换", public="配置已变更，请重试",
        )
    return snapshot


# ── 契约校验 ──────────────────────────────────────────────

BLOCK_SOURCE_KEYS = {
    "account": ("mode", "balances"),
    "positions": ("positions", "orders", "market"),
    "funding": ("positions", "market", "user_funding"),
    "ratio": ("market", "candles_first", "candles_second"),
    "unlocks": ("config",),
    "announcements": ("general", "api"),
}

BLOCK_DATA_KEYS = {
    "account": ("account_mode", "equity", "equity_scope"),
    "positions": ("items", "position_state", "unrealized_pnl", "notional_ratio",
                  "ratio_drift_pct", "open_orders"),
    "funding": ("hourly_rates", "annualized_pct", "daily_estimate", "window_start",
                "window_end", "window_complete", "actual_48h_net", "recent_payments"),
    "ratio": ("live", "latest_close", "as_of_date", "ma30", "vs_ma30_pct", "points"),
    "unlocks": ("anchors", "note"),
}


def _degraded_block(name: str, reason: str, fallback_queried_at: str, coins: tuple = ()) -> dict:
    """按 block 类型的完整降级 DTO：字段齐全、未知值为 null、固定 source keys 保留。

    不允许保留未验证的原始值；与 unlocks_enabled=false 时的 unlocks=null 语义区分。
    """
    sources = {key: {"status": "unavailable", "queried_at": None}
               for key in BLOCK_SOURCE_KEYS[name]}
    data = {
        "account": {"account_mode": None, "equity": None, "equity_scope": "unavailable"},
        "positions": {"items": None, "position_state": "unknown", "unrealized_pnl": None,
                      "notional_ratio": None, "ratio_drift_pct": None, "open_orders": None},
        "funding": {"hourly_rates": {coin: None for coin in coins},
                    "annualized_pct": {coin: None for coin in coins},
                    "daily_estimate": None, "window_start": None, "window_end": None,
                    "window_complete": False, "actual_48h_net": None, "recent_payments": None},
        "ratio": {"live": None, "latest_close": None, "as_of_date": None, "ma30": None,
                  "vs_ma30_pct": None, "points": None},
        "unlocks": {"anchors": [], "note": "日历预告不等于实际申领已发生"},
    }[name]
    block = {"status": "unavailable", "queried_at": fallback_queried_at,
             "source_queries": sources, "diagnostics": [reason]}
    block.update(data)
    return block


def _valid_block_wrapper(block, fallback_queried_at: str, name: str) -> bool:
    if not isinstance(block, dict):
        return False
    if block.get("status") not in STATUS_VALUES:
        return False
    if not _timestamp(block.get("queried_at")):
        return False
    sources = block.get("source_queries")
    if not isinstance(sources, dict) or set(sources) != set(BLOCK_SOURCE_KEYS[name]):
        return False
    for query in sources.values():
        if not isinstance(query, dict) or set(query) != {"status", "queried_at"}:
            return False
        if query.get("status") not in ("ok", "unavailable"):
            return False
        when = query.get("queried_at")
        if when is not None and not _timestamp(when):
            return False
        if query["status"] == "ok" and when is None:
            return False
    diagnostics = block.get("diagnostics")
    if not isinstance(diagnostics, list) or not all(isinstance(d, str) for d in diagnostics):
        return False
    if block["status"] == "ok" and diagnostics:
        return False
    return True


def _valid_position(item, coins: set) -> bool:
    if not isinstance(item, dict):
        return False
    coin = item.get("coin")
    if not isinstance(coin, str) or coin not in coins or item.get("side") not in SIDES:
        return False
    if not _non_negative_decimal(item.get("size")):
        return False
    for key in ("entry_px", "mark_px", "unrealized_pnl", "margin_used", "liquidation_px"):
        if item.get(key) is not None and not _decimal_string(item[key]):
            return False
    if item.get("notional") is not None and not _non_negative_decimal(item["notional"]):
        return False
    if item.get("margin_mode") is not None and item.get("margin_mode") not in MARGIN_MODES:
        return False
    return True


def _validate_block_data(name: str, data, coins: tuple) -> str | None:
    """返回失败原因；None 表示通过。"""
    if not isinstance(data, dict):
        return f"{name} 数据不是对象"
    missing = set(BLOCK_DATA_KEYS[name]) - set(data)
    if missing:
        return f"{name} 数据缺少字段: {sorted(missing)}"
    if name == "account":
        if data.get("account_mode") is not None and not isinstance(data["account_mode"], str):
            return "account_mode 类型错误"
        if data.get("equity") is not None and not _decimal_string(data["equity"]):
            return "equity 不是有限十进制字符串"
        if data.get("equity_scope") not in EQUITY_SCOPES:
            return "equity_scope 枚举非法"
    elif name == "positions":
        items = data.get("items")
        if items is not None:
            if not isinstance(items, list) or any(not _valid_position(i, set(coins)) for i in items):
                return "持仓条目非法"
        if data.get("position_state") not in POSITION_STATES:
            return "position_state 枚举非法"
        for key in ("unrealized_pnl", "notional_ratio", "ratio_drift_pct"):
            if data.get(key) is not None and not _decimal_string(data[key]):
                return f"{key} 不是有限十进制字符串"
        if data.get("open_orders") is not None and (
                type(data["open_orders"]) is not int or data["open_orders"] < 0):
            return "open_orders 非法"
    elif name == "funding":
        coin_set = set(coins)
        for key in ("hourly_rates", "annualized_pct"):
            rates = data.get(key)
            if (not isinstance(rates, dict) or set(rates) != coin_set
                    or any(v is not None and not _decimal_string(v) for v in rates.values())):
                return f"{key} 必须恰好覆盖配置两腿且为有限十进制或 null"
        estimate = data.get("daily_estimate")
        if estimate is not None:
            if (not isinstance(estimate, dict)
                    or estimate.get("direction") not in DIRECTIONS
                    or not _non_negative_decimal(estimate.get("amount"))):
                return "daily_estimate 非法"
        start, end = data.get("window_start"), data.get("window_end")
        if not _timestamp(start) or not _timestamp(end):
            return "资金费窗口起止必须是 UTC 时间"
        start_dt = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
        end_dt = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
        if not start_dt < end_dt:
            return "资金费窗口起止反向"
        if (end_dt - start_dt).total_seconds() != 48 * 3600:
            return "资金费窗口不是恰好 48 小时"
        if type(data.get("window_complete")) is not bool:
            return "window_complete 必须是布尔值"
        actual = data.get("actual_48h_net")
        if actual is not None and not _decimal_string(actual):
            return "actual_48h_net 不是有限十进制字符串"
        payments = data.get("recent_payments")
        if (actual is None) != (payments is None):
            return "实际净收付与最近收付必须同时已知或同时未知"
        if data["window_complete"] and actual is None:
            return "完整资金费历史不能把未知收付伪装为空值"
        if payments is not None:
            if not isinstance(payments, list):
                return "recent_payments 非法"
            if len(payments) > 3:
                return "recent_payments 最多 3 条"
            parsed_times = []
            for p in payments:
                if (not isinstance(p, dict)
                        or not isinstance(p.get("coin"), str)
                        or p.get("coin") not in coin_set
                        or not _timestamp(p.get("time"))
                        or not _decimal_string(p.get("usdc"))
                        or not _decimal_string(p.get("rate"))):
                    return "recent_payments 非法"
                parsed_times.append(datetime.strptime(p["time"], "%Y-%m-%dT%H:%M:%SZ"))
            if any(not (start_dt <= t < end_dt) for t in parsed_times):
                return "收付明细时间不在窗口内"
            if any(parsed_times[i] < parsed_times[i + 1] for i in range(len(parsed_times) - 1)):
                return "收付明细必须按时间倒序"
    elif name == "ratio":
        for key in ("live", "latest_close", "ma30", "vs_ma30_pct"):
            if data.get(key) is not None and not _decimal_string(data[key]):
                return f"{key} 不是有限十进制字符串"
        if data.get("as_of_date") is not None and not _date_string(data["as_of_date"]):
            return "as_of_date 不是合法日期"
        points = data.get("points")
        if points is not None:
            if not isinstance(points, list):
                return "比价序列必须是数组或 null"
            for point in points:
                if (not isinstance(point, dict)
                        or not _date_string(point.get("date"))
                        or any(point.get(k) is not None and not _decimal_string(point[k])
                               for k in ("first_close", "second_close", "ratio", "ma30"))):
                    return "比价序列点非法"
    elif name == "unlocks":
        anchors = data.get("anchors")
        if not isinstance(anchors, list):
            return "anchors 必须是数组"
        for anchor in anchors:
            if (not isinstance(anchor, dict)
                    or not _date_string(anchor.get("date"))
                    or type(anchor.get("days_ahead")) is not int or anchor["days_ahead"] < 0
                    or not isinstance(anchor.get("label"), str)
                    or not isinstance(anchor.get("source_url"), str)
                    or not _date_string(anchor.get("verified_utc"))):
                return "解锁锚点非法"
        if not isinstance(data.get("note"), str):
            return "note 必须是字符串"
    return None


def _validate_v2_snapshot(snapshot: dict, descriptor: dict, revision: str) -> dict:
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 2:
        raise HyperliquidUnavailable(
            f"schema_version 不是 2: {snapshot.get('schema_version')!r}",
            public="快照版本不受支持",
        )
    if snapshot.get("config_revision") != revision:
        raise HyperliquidUnavailable(
            "快照 config_revision 与当前配置不一致", public="配置已变更，请重试",
        )
    if not _timestamp(snapshot.get("queried_at")):
        raise HyperliquidUnavailable("快照缺少 queried_at", public="快照缺少 queried_at")

    pair = snapshot.get("pair")
    expected_account = (descriptor["account_address"] or "").lower()
    if (not isinstance(pair, dict)
            or pair.get("id") != descriptor["id"]
            or pair.get("network") != descriptor["network"]
            or pair.get("dex") != descriptor["dex"]
            or pair.get("coins") != list(descriptor["coins"])
            or str(pair.get("account_address") or "").lower() != expected_account):
        raise HyperliquidUnavailable(
            "快照 pair 身份与目录不符", public="快照身份与配置不符",
        )

    blocks = snapshot.get("blocks")
    if not isinstance(blocks, dict):
        raise HyperliquidUnavailable("快照缺少 blocks", public="快照结构非法")
    for name in BLOCK_NAMES:
        if name not in blocks:
            blocks[name] = _degraded_block(
                name, f"快照缺少 {name} 区块", snapshot["queried_at"], tuple(descriptor["coins"]))

    coins = tuple(descriptor["coins"])
    applicable_unavailable = 0
    for name in BLOCK_NAMES:
        block = blocks[name]
        if name == "unlocks" and not descriptor["unlocks_enabled"]:
            blocks[name] = None  # 不适用始终为 null，不用错误对象替代
            continue
        reason = None
        if block is None:
            reason = f"{name} 区块为 null"
        elif not _valid_block_wrapper(block, snapshot["queried_at"], name):
            reason = f"{name} 区块结构非法"
        else:
            data = {k: v for k, v in block.items()
                    if k not in ("status", "queried_at", "source_queries", "diagnostics")}
            reason = _validate_block_data(name, data, coins)
            if (name == "funding" and reason is None
                    and data.get("window_complete") is False and block["status"] == "ok"):
                reason = "窗口不完整时资金费区块不能为 ok"
        if reason:
            block = _degraded_block(name, reason, snapshot["queried_at"], coins)
            blocks[name] = block
        if name != "unlocks" and block["status"] == "unavailable":
            applicable_unavailable += 1
    if applicable_unavailable == len(BLOCK_NAMES) - 1:
        raise HyperliquidUnavailable("全部适用区块不可用", public="看板数据暂不可用")
    return snapshot


def _validate_announcements(payload: dict, revision: str) -> dict:
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 2:
        raise HyperliquidUnavailable("公告 schema_version 不是 2", public="快照版本不受支持")
    if not _timestamp(payload.get("queried_at")):
        raise HyperliquidUnavailable("公告缺少 queried_at", public="快照缺少 queried_at")
    block = payload.get("block")
    if not _valid_block_wrapper(block, payload["queried_at"], "announcements"):
        raise HyperliquidUnavailable("公告区块结构非法", public="快照结构非法")
    items = block.get("items")
    if items is not None:
        if not isinstance(items, list):
            raise HyperliquidUnavailable("公告条目必须是数组或 null", public="快照结构非法")
        for item in items:
            if (not isinstance(item, dict)
                    or item.get("kind") not in ("general", "api")
                    or not isinstance(item.get("text"), str)
                    or (item.get("published_at") is not None and not _timestamp(item["published_at"]))
                    or not isinstance(item.get("url"), str)
                    or not isinstance(item.get("source"), str)):
                raise HyperliquidUnavailable("公告条目非法", public="快照结构非法")
    return payload


# ── 缓存与入口 ────────────────────────────────────────────


def _cache_key(kind: str, revision: str, descriptor: dict | None) -> str:
    if descriptor is None:
        return f"announcements:{revision}"
    account = descriptor["account_address"] or "null"
    return ":".join([kind, revision, descriptor["network"], account,
                     descriptor["dex"], ",".join(descriptor["coins"]), descriptor["id"]])


def _cached(cache: dict, key: str):
    entry = cache.get(key)
    if entry and time.monotonic() < entry["expires_at"]:
        return entry["snapshot"]
    return None


def get_pair_dashboard(pair_id: str) -> dict:
    revision, pairs = load_pairs()
    descriptor = next((p for p in pairs if p["id"] == pair_id), None)
    if descriptor is None:
        raise LookupError(f"未知或停用的 pair: {pair_id}")
    key = _cache_key("pair", revision, descriptor)
    lock = _key_lock(key)
    with lock:
        hit = _cached(_snapshot_cache, key)
        if hit is not None:
            return hit
        with _collector_sem:
            snapshot = _run_dashboard(["--pair", pair_id])
            snapshot = _validate_v2_snapshot(snapshot, descriptor, revision)
            _snapshot_cache[key] = {
                "snapshot": snapshot,
                "expires_at": time.monotonic() + SNAPSHOT_CACHE_TTL_SECONDS,
            }
            return snapshot


def get_announcements() -> dict:
    revision, _pairs = load_pairs()
    key = _cache_key("announcements", revision, None)
    lock = _key_lock(key)
    with lock:
        hit = _cached(_announcements_cache, key)
        if hit is not None:
            return hit
        with _collector_sem:
            payload = _run_dashboard(["--announcements"])
            payload = _validate_announcements(payload, revision)
            _announcements_cache[key] = {
                "snapshot": payload,
                "expires_at": time.monotonic() + SNAPSHOT_CACHE_TTL_SECONDS,
            }
            return payload
