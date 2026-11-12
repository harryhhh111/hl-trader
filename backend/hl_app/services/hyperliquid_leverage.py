from __future__ import annotations

import hashlib
import json
import os
import subprocess
from decimal import Decimal

from psycopg2.extras import Json, RealDictCursor

from ..auth import SessionInfo
from ..db import connect
from . import hyperliquid_adjustments as adjustments
from . import hyperliquid_managed_adjustments as managed
from . import hyperliquid_pairs as pairs_service
from . import hyperliquid_wallets as wallets
from . import hyperliquid_wallet_authorization as authorization
from . import hyperliquid_wallet_crypto as wallet_crypto
from .process import communicate_bounded


def _validated(descriptor: dict, account: str, coin: str, leverage: int, margin_mode: str) -> None:
    if coin not in descriptor["coins"]:
        raise wallets.WalletError(422, "合约不属于当前 pair")
    if type(leverage) is not int or not 1 <= leverage <= 3:
        raise wallets.WalletError(422, "杠杆必须是 1 到 3 的正整数，本应用上限为 3x")
    if margin_mode not in {"cross", "isolated"}:
        raise wallets.WalletError(422, "保证金模式只支持 cross 或 isolated")


def _argv(descriptor: dict, account: str, command: str, coin: str, leverage: int, margin_mode: str) -> list[str]:
    binary = os.environ.get("HL_APP_TRADER_BIN", "").strip()
    config = os.environ.get("HL_APP_PAIRS_CONFIG", "").strip()
    if not binary or not config:
        raise wallets.WalletError(503, "杠杆执行器未配置")
    return [
        binary, "--config", config, command, "--pair", descriptor["id"], "--address", account,
        "--coin", coin, "--leverage", str(leverage), "--margin-mode", margin_mode,
        "--state-dir", adjustments._state_dir(), "--json",
    ]


def _invoke(argv: list[str], descriptor: dict, account: str, coin: str, leverage: int, margin_mode: str) -> dict:
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise wallets.WalletError(503, "无法启动杠杆执行器") from exc
    try:
        stdout, stderr = communicate_bounded(proc, adjustments.EXECUTE_TIMEOUT_SECONDS)
    except Exception as exc:
        raise wallets.WalletError(503, "杠杆执行超时或结果未知，请按 request_id 和回执核对") from exc
    try:
        report = json.loads(stdout)
    except json.JSONDecodeError:
        report = None
    if isinstance(report, dict) and report.get("schema_version") == 1:
        if (
            report.get("pair_id") != descriptor["id"] or report.get("network") != descriptor["network"]
            or report.get("account_address") != account or report.get("coin") != coin
            or report.get("leverage") != leverage or report.get("margin_mode") != margin_mode
            or report.get("status") not in {"prepared", "executing", "succeeded", "rejected", "unknown"}
        ):
            raise wallets.WalletError(503, "杠杆结果身份校验失败")
        return report
    if proc.returncode == 2:
        detail = stderr.strip().removeprefix("错误: ")[:500]
        raise wallets.WalletError(422, detail or "杠杆调整被拒绝")
    raise wallets.WalletError(503, "杠杆执行器未返回可信结果；如已提交，请先核对回执")


def prepare_browser(
    url: str, pair_id: str, account_address: str, config_revision: str, coin: str,
    leverage: int, margin_mode: str, expected_signer: str, owner_account_id: int | None = None,
) -> dict:
    descriptor, account = adjustments._resolve(url, pair_id, account_address, config_revision, owner_account_id)
    _validated(descriptor, account, coin, leverage, margin_mode)
    signer = adjustments._browser_signer(account, expected_signer)
    argv = _argv(descriptor, account, "leverage-prepare", coin, leverage, margin_mode)
    argv.extend(["--signer", signer])
    report = _invoke(argv, descriptor, account, coin, leverage, margin_mode)
    if report.get("status") != "prepared" or report.get("expected_signer") != signer or not report.get("typed_data"):
        raise wallets.WalletError(503, "杠杆签名准备结果不完整")
    return report


def submit_browser(
    url: str, pair_id: str, account_address: str, config_revision: str, coin: str,
    leverage: int, margin_mode: str, expected_signer: str, action_id: str, signature: str,
    owner_account_id: int | None = None,
) -> dict:
    descriptor, account = adjustments._resolve(url, pair_id, account_address, config_revision, owner_account_id)
    _validated(descriptor, account, coin, leverage, margin_mode)
    signer = adjustments._browser_signer(account, expected_signer)
    if not action_id.startswith("leverage-0x") or len(action_id) != 43 or any(c not in "0123456789abcdef" for c in action_id[11:]):
        raise wallets.WalletError(422, "杠杆动作 ID 非法")
    checked = adjustments._validated_signatures([{"leg_index": 0, "signature": signature}])[0]["signature"]
    argv = _argv(descriptor, account, "leverage-submit", coin, leverage, margin_mode)
    argv.extend(["--signer", signer, "--action-id", action_id, "--signature", checked])
    try:
        report = _invoke(argv, descriptor, account, coin, leverage, margin_mode)
    finally:
        pairs_service.clear_runtime_cache()
    if report.get("action_id") != action_id or report.get("expected_signer") != signer:
        raise wallets.WalletError(503, "杠杆提交结果身份不符，请先核对回执")
    return report


def _payload_hash(
    descriptor: dict, account: str, config_revision: str, coin: str, leverage: int,
    margin_mode: str, wallet_version: int,
) -> str:
    body = {
        "operation": "update_leverage",
        "network": descriptor["network"], "pair_id": descriptor["id"], "account_address": account,
        "config_revision": config_revision, "coin": coin, "leverage": leverage,
        "margin_mode": margin_mode, "wallet_version": wallet_version,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def execute_managed(
    url: str, pair_id: str, account_address: str, config_revision: str, coin: str,
    leverage: int, margin_mode: str, confirmation: str, request_id: str, wallet_version: int,
    session: SessionInfo,
) -> tuple[dict, int]:
    request_id = managed._request_id(request_id)
    if confirmation.strip() != f"{coin} {margin_mode} {leverage}x":
        raise wallets.WalletError(422, "确认内容必须与合约、保证金模式和杠杆完全一致")
    descriptor, account = adjustments._resolve(url, pair_id, account_address, config_revision, session.account_id)
    _validated(descriptor, account, coin, leverage, margin_mode)
    if type(wallet_version) is not int or wallet_version < 1:
        raise wallets.WalletError(422, "托管钱包版本非法")
    payload_hash = _payload_hash(descriptor, account, config_revision, coin, leverage, margin_mode, wallet_version)
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            wallets._lock_scope(cursor, descriptor["network"], pair_id, account)
            row = managed._matching_existing(
                managed._request_row(cursor, request_id, lock=True, owner_account_id=session.account_id),
                session, payload_hash,
            )
            if row is not None:
                return managed._view(row), 202 if row["status"] == "running" else 200
            wallet = managed._active_wallet(cursor, descriptor, account, wallet_version)
            if wallets._active_execution(cursor, descriptor["network"], pair_id, account):
                raise wallets.WalletError(409, "该账户已有执行中或待核对的操作")
            initial = {
                "schema_version": 1, "status": "executing", "action_id": None,
                "pair_id": pair_id, "network": descriptor["network"], "account_address": account,
                "config_revision": config_revision, "coin": coin, "leverage": leverage,
                "margin_mode": margin_mode, "request_id": request_id,
            }
            cursor.execute(
                """
                INSERT INTO hyperliquid_execution_requests (
                    request_id, actor, network, pair_id, account_address, config_revision,
                    target_notional, request_payload_hash, preview_hash, wallet_address,
                    wallet_version, status, result, started_at, owner_account_id
                ) VALUES (%s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, 'running', %s, now(), %s)
                RETURNING *
                """,
                (
                    request_id, wallets._actor(session), descriptor["network"], pair_id, account,
                    config_revision, payload_hash, payload_hash, wallet["wallet_address"], wallet_version,
                    Json(initial), session.account_id,
                ),
            )
            row = dict(cursor.fetchone())
            wallets._event(cursor, "execute_requested", row, row["actor"], request_id, "running")
    if os.environ.get("HL_APP_TRADING_ENABLED", "").strip().lower() != "true":
        return managed._finish(url, request_id, "failed", "trading_disabled", "真实交易未开启，未调整杠杆"), 200
    try:
        authorization.verify_authorization(descriptor["network"], account, wallet["wallet_address"])
        private_key = wallet_crypto.decrypt_storage(
            wallet["encrypted_private_key"], descriptor["network"], pair_id, account,
            wallet["wallet_address"], wallet_version,
        )
        argv = _argv(descriptor, account, "leverage-execute", coin, leverage, margin_mode)
        argv.extend(["--secret-fd", "0", "--request-id", request_id])
        if not hasattr(os, "memfd_create"):
            raise wallet_crypto.WalletCryptoError(503, "当前系统不支持安全的托管私钥传递")
        fd = os.memfd_create("hl-managed-leverage", flags=os.MFD_CLOEXEC)
        try:
            encoded = private_key.encode("ascii")
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.lseek(fd, 0, os.SEEK_SET)
            argv[argv.index("--secret-fd") + 1] = str(fd)
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=(fd,))
            stdout, stderr = communicate_bounded(proc, adjustments.EXECUTE_TIMEOUT_SECONDS)
        finally:
            os.close(fd)
            pairs_service.clear_runtime_cache()
        try:
            report = json.loads(stdout)
        except json.JSONDecodeError:
            report = None
        if not isinstance(report, dict) or report.get("schema_version") != 1:
            if proc.returncode == 2:
                detail = stderr.strip().removeprefix("错误: ")[:500]
                return managed._finish(url, request_id, "failed", "execution_refused", f"未调整杠杆：{detail}"), 200
            raise wallets.WalletError(503, "杠杆执行器未返回可信结果")
        if (
            report.get("request_id") != request_id or report.get("pair_id") != pair_id
            or report.get("account_address") != account or report.get("coin") != coin
            or report.get("leverage") != leverage or report.get("margin_mode") != margin_mode
        ):
            raise wallets.WalletError(503, "杠杆结果身份校验失败")
        status = {"succeeded": "succeeded", "rejected": "incomplete", "unknown": "unknown"}.get(report.get("status"), "unknown")
        return managed._finish(url, request_id, status, None, None, report), 200
    except (authorization.AuthorizationError, wallet_crypto.WalletCryptoError) as exc:
        return managed._finish(url, request_id, "failed", "preflight_failed", f"未调整杠杆：{exc.detail}"), 200
    except wallets.WalletError as exc:
        if exc.status_code < 500:
            return managed._finish(url, request_id, "failed", "execution_refused", exc.detail), 200
        return managed._finish(url, request_id, "unknown", "execution_unresolved", exc.detail), 200
    except Exception:
        return managed._finish(
            url, request_id, "unknown", "execution_unresolved",
            "杠杆执行异常；结果未知，请按 request_id 和回执核对，禁止直接重试",
        ), 200
