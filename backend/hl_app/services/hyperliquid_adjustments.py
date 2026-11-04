from __future__ import annotations

import json
import os
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import hyperliquid_accounts as accounts_service
from . import hyperliquid_pairs as pairs_service
from .process import HyperliquidUnavailable, communicate_bounded

PREVIEW_TIMEOUT_SECONDS = 45
EXECUTE_TIMEOUT_SECONDS = 120


class AdjustmentError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _decimal(value: str, label: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise AdjustmentError(422, f"{label} 必须是十进制数") from exc
    if not parsed.is_finite():
        raise AdjustmentError(422, f"{label} 必须是有限十进制数")
    return parsed


def _decimal_arg(value: Decimal) -> str:
    return format(value, "f")


def _maximum() -> Decimal:
    value = _decimal(os.environ.get("HL_APP_MAX_LEG_NOTIONAL_USD", "15000"), "每腿名义上限")
    if value <= 0:
        raise AdjustmentError(503, "HL_APP_MAX_LEG_NOTIONAL_USD 配置非法")
    return value


def _execution_ready() -> bool:
    return (
        os.environ.get("HL_APP_TRADING_ENABLED", "").strip().lower() == "true"
        and bool(os.environ.get("HL_APP_AGENT_SECRET_FILE", "").strip())
        and bool(os.environ.get("HL_APP_EXECUTION_STATE_DIR", "").strip())
    )


def _resolve(url: str, pair_id: str, account_address: str, config_revision: str) -> tuple[dict, str]:
    revision, descriptors = pairs_service.load_pairs()
    if revision != config_revision:
        raise AdjustmentError(409, "pair 配置已变化，请刷新页面后重新预览")
    descriptor = next((item for item in descriptors if item["id"] == pair_id), None)
    if descriptor is None:
        raise AdjustmentError(404, f"未知或停用的 pair: {pair_id}")
    active = accounts_service.apply_active_accounts(url, [descriptor])[0]
    expected = active["account_address"]
    supplied = accounts_service.normalize_address(account_address)
    if expected is None or supplied != expected:
        raise AdjustmentError(409, "当前账户已变化，请刷新页面后重新预览")
    return active, supplied


def _parse_report(
    stdout: str, descriptor: dict, account: str, target: Decimal, config_revision: str | None = None,
) -> dict | None:
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        return None
    if report.get("status") not in {"preview", "prepared", "succeeded", "incomplete", "unknown"}:
        return None
    if (
        report.get("pair_id") != descriptor["id"]
        or report.get("network") != descriptor["network"]
        or report.get("account_address") != account
        or report.get("config_revision") != (config_revision or pairs_service.load_pairs()[0])
        or _decimal_arg(_decimal(str(report.get("target_notional", "")), "目标名义")) != _decimal_arg(target)
        or not isinstance(report.get("legs"), list)
        or len(report["legs"]) != 2
    ):
        raise AdjustmentError(503, "调仓结果身份校验失败")
    if {leg.get("coin") for leg in report["legs"] if isinstance(leg, dict)} != set(descriptor["coins"]):
        raise AdjustmentError(503, "调仓结果合约身份校验失败")
    return report


def _invoke(
    argv: list[str], descriptor: dict, account: str, target: Decimal, execute: bool,
    pass_fds: tuple[int, ...] = (),
) -> dict:
    config_revision = pairs_service.load_pairs()[0]
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=pass_fds
        )
    except OSError as exc:
        raise AdjustmentError(503, "无法启动调仓执行器") from exc
    try:
        stdout, stderr = communicate_bounded(
            proc, EXECUTE_TIMEOUT_SECONDS if execute else PREVIEW_TIMEOUT_SECONDS,
        )
    except HyperliquidUnavailable as exc:
        detail = "调仓进程中断，订单结果未知；禁止重试，请先核对交易所记录" if execute else "调仓预览或签名准备超时"
        raise AdjustmentError(503, detail) from exc
    report = _parse_report(stdout, descriptor, account, target, config_revision)
    if report is not None:
        return report
    if proc.returncode == 2:
        detail = stderr.strip().removeprefix("错误: ")[:500]
        raise AdjustmentError(422, detail or "调仓请求被拒绝")
    if execute:
        raise AdjustmentError(503, "调仓执行器未能返回可信结果；订单状态可能未知，请先核对，禁止直接重试")
    raise AdjustmentError(503, "调仓预览或签名准备失败，请稍后重试")


def _base_argv(descriptor: dict, account: str, target: Decimal, command: str) -> list[str]:
    binary = os.environ.get("HL_APP_TRADER_BIN", "").strip()
    config = os.environ.get("HL_APP_PAIRS_CONFIG", "").strip()
    if not binary or not config:
        raise AdjustmentError(503, "调仓执行器未配置")
    return [
        binary, "--config", config, command, "--pair", descriptor["id"],
        "--address", account, "--notional", _decimal_arg(target),
        "--max-notional", _decimal_arg(_maximum()), "--json",
    ]


def _state_dir() -> str:
    state_dir = os.environ.get("HL_APP_EXECUTION_STATE_DIR", "").strip()
    if not state_dir or not Path(state_dir).is_absolute():
        raise AdjustmentError(503, "HL_APP_EXECUTION_STATE_DIR 必须配置为绝对路径")
    return state_dir


def _run(descriptor: dict, account: str, target: Decimal, execute: bool) -> dict:
    argv = _base_argv(descriptor, account, target, "adjust")
    if execute:
        if not _execution_ready():
            raise AdjustmentError(503, "真实调仓未开启或 agent/回执目录未配置")
        secret_file = os.environ["HL_APP_AGENT_SECRET_FILE"].strip()
        argv.extend(["--secret-file", secret_file, "--state-dir", _state_dir(), "--execute"])
    return _invoke(argv, descriptor, account, target, execute)


def preview(
    url: str, pair_id: str, account_address: str, config_revision: str, target_notional: str,
) -> dict:
    target = _decimal(target_notional, "每腿目标名义")
    if target < 0 or target > Decimal("1000000"):
        raise AdjustmentError(422, "每腿目标名义必须在 0 到 1000000 USD 之间")
    descriptor, account = _resolve(url, pair_id, account_address, config_revision)
    report = _run(descriptor, account, target, execute=False)
    report["execution_enabled"] = _execution_ready()
    report["browser_wallet_enabled"] = True
    report["max_leg_notional_usd"] = _decimal_arg(_maximum())
    return report


def execute(
    url: str,
    pair_id: str,
    account_address: str,
    config_revision: str,
    target_notional: str,
    confirmation: str,
) -> dict:
    target = _decimal(target_notional, "每腿目标名义")
    if target < 0 or target > Decimal("1000000"):
        raise AdjustmentError(422, "每腿目标名义必须在 0 到 1000000 USD 之间")
    if confirmation.strip() != _decimal_arg(target):
        raise AdjustmentError(422, "确认金额必须与每腿目标名义完全一致")
    descriptor, account = _resolve(url, pair_id, account_address, config_revision)
    try:
        return _run(descriptor, account, target, execute=True)
    finally:
        pairs_service.clear_runtime_cache()


def _browser_signer(account: str, expected_signer: str) -> str:
    signer = accounts_service.normalize_address(expected_signer)
    if signer == account:
        raise AdjustmentError(422, "API Wallet 必须不同于原交易账户")
    return signer


def prepare_browser(
    url: str,
    pair_id: str,
    account_address: str,
    config_revision: str,
    target_notional: str,
    expected_signer: str,
) -> dict:
    target = _decimal(target_notional, "每腿目标名义")
    if target < 0 or target > Decimal("1000000"):
        raise AdjustmentError(422, "每腿目标名义必须在 0 到 1000000 USD 之间")
    descriptor, account = _resolve(url, pair_id, account_address, config_revision)
    signer = _browser_signer(account, expected_signer)
    argv = _base_argv(descriptor, account, target, "adjust-prepare")
    argv.extend(["--state-dir", _state_dir(), "--signer", signer])
    report = _invoke(argv, descriptor, account, target, execute=False)
    if report.get("expected_signer") != signer or not report.get("action_id"):
        raise AdjustmentError(503, "待签名结果的 API Wallet 或动作身份不符")
    return report


def _validated_signatures(value) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 2:
        raise AdjustmentError(422, "必须提供一到两份订单签名")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"leg_index", "signature"}:
            raise AdjustmentError(422, "签名条目格式非法")
        leg_index = item["leg_index"]
        signature = item["signature"]
        if type(leg_index) is not int or leg_index not in {0, 1}:
            raise AdjustmentError(422, "签名腿序号非法")
        if (
            not isinstance(signature, str)
            or len(signature) != 132
            or not signature.startswith("0x")
            or any(character not in "0123456789abcdefABCDEF" for character in signature[2:])
        ):
            raise AdjustmentError(422, "签名必须是 65 字节十六进制数据")
        result.append({"leg_index": leg_index, "signature": signature})
    return result


def submit_browser(
    url: str,
    pair_id: str,
    account_address: str,
    config_revision: str,
    target_notional: str,
    expected_signer: str,
    action_id: str,
    signatures: list[dict],
) -> dict:
    target = _decimal(target_notional, "每腿目标名义")
    if target < 0 or target > Decimal("1000000"):
        raise AdjustmentError(422, "每腿目标名义必须在 0 到 1000000 USD 之间")
    descriptor, account = _resolve(url, pair_id, account_address, config_revision)
    signer = _browser_signer(account, expected_signer)
    checked = _validated_signatures(signatures)
    if (
        len(action_id) != 41
        or not action_id.startswith("adjust-0x")
        or any(character not in "0123456789abcdef" for character in action_id[9:])
    ):
        raise AdjustmentError(422, "调仓动作 ID 非法")
    argv = _base_argv(descriptor, account, target, "adjust-submit")
    argv.extend([
        "--state-dir", _state_dir(), "--signer", signer,
        "--action-id", action_id, "--signatures", json.dumps(checked, separators=(",", ":")),
    ])
    try:
        report = _invoke(argv, descriptor, account, target, execute=True)
    finally:
        pairs_service.clear_runtime_cache()
    if report.get("expected_signer") != signer or report.get("action_id") != action_id:
        raise AdjustmentError(503, "签名提交结果身份不符；请先核对订单状态，禁止直接重试")
    return report
