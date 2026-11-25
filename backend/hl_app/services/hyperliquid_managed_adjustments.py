from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path

from psycopg2.extras import Json, RealDictCursor

from ..auth import SessionInfo
from ..db import connect
from . import hyperliquid_adjustments as adjustments
from . import hyperliquid_pairs as pairs_service
from . import hyperliquid_wallets as wallets
from . import hyperliquid_wallet_authorization as authorization
from . import hyperliquid_wallet_crypto as wallet_crypto

TERMINAL_STATUSES = {"succeeded", "incomplete", "failed"}


def _request_id(value: str) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise wallets.WalletError(422, "调仓 request_id 必须是 UUID") from exc
    if normalized != value.lower():
        raise wallets.WalletError(422, "调仓 request_id 必须是规范 UUID")
    return normalized


def _payload_hash(
    descriptor: dict, account: str, target: Decimal, config_revision: str, wallet_version: int,
    leg_indices: list[int], max_slippage_bps: int = 5,
) -> str:
    body = {
        "network": descriptor["network"],
        "pair_id": descriptor["id"],
        "account_address": account,
        "target_notional": adjustments._decimal_arg(target),
        "config_revision": config_revision,
        "wallet_version": wallet_version,
        "leg_indices": leg_indices,
        "max_slippage_bps": adjustments._slippage(max_slippage_bps),
    }
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preview_hash(report: dict) -> str:
    encoded = json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _request_row(cursor, request_id: str, lock: bool = False, owner_account_id: int | None = None) -> dict | None:
    ownership = "" if owner_account_id is None else " AND owner_account_id = %s"
    params = (request_id,) if owner_account_id is None else (request_id, owner_account_id)
    cursor.execute(
        f"SELECT * FROM hyperliquid_execution_requests WHERE request_id = %s{ownership}{' FOR UPDATE' if lock else ''}",
        params,
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _view(row: dict) -> dict:
    result = dict(row["result"] or {})
    result.update({
        "request_id": str(row["request_id"]),
        "wallet_address": row["wallet_address"],
        "wallet_version": row["wallet_version"],
        "managed_status": row["status"],
        "action_id": row["action_id"] or result.get("action_id"),
        "error_code": row["error_code"],
    })
    return result


def _receipt_exists(pair_id: str, account: str, request_id: str) -> bool:
    directory = Path(adjustments._state_dir()) / pair_id / account
    try:
        paths = [
            path for path in directory.iterdir()
            if path.name.startswith("adjust") and path.suffix == ".json"
        ]
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise wallets.WalletError(503, "执行回执暂不可读取，禁止自动解除核对状态") from exc
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError("invalid execution receipt")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("invalid execution receipt")
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise wallets.WalletError(409, "执行回执无法完整核对，禁止自动解除核对状态") from exc
        if value.get("request_id") == request_id:
            return True
    return False


def _require_pre_send_unknown(row: dict) -> None:
    result = row["result"] if isinstance(row["result"], dict) else {}
    if row["action_id"] or result.get("action_id"):
        raise wallets.WalletError(409, "该请求已有动作 ID，不能按未发送解除；请按订单回执人工核对")
    legs = result.get("legs")
    if not isinstance(legs, list) or len(legs) != 2:
        raise wallets.WalletError(409, "该请求缺少完整的两腿预览，不能按未发送解除")
    for leg in legs:
        if not isinstance(leg, dict) or leg.get("status") not in {"prepared", "not_sent"}:
            raise wallets.WalletError(409, "该请求已进入订单执行阶段，不能按未发送解除")
        if any(leg.get(key) is not None for key in ("cloid", "nonce", "oid")):
            raise wallets.WalletError(409, "该请求已有订单标识，不能按未发送解除；请按订单回执人工核对")
        if adjustments._decimal(str(leg.get("confirmed_qty", "")), "成交数量") != Decimal("0"):
            raise wallets.WalletError(409, "该请求已有成交数量，不能按未发送解除")


def _matching_existing(row: dict | None, session: SessionInfo, payload_hash: str) -> dict | None:
    if row is None:
        return None
    if row["actor"] != wallets._actor(session) or row["request_payload_hash"] != payload_hash:
        raise wallets.WalletError(409, "request_id 已用于其他调仓请求")
    return row


def _active_wallet(cursor, descriptor: dict, account: str, wallet_version: int) -> dict:
    row = wallets._wallet_row(cursor, descriptor["network"], descriptor["id"], account, lock=True)
    if (
        row is None or row["status"] != "active" or row["wallet_version"] != wallet_version
        or row["encrypted_private_key"] is None
    ):
        raise wallets.WalletError(409, "托管 API Wallet 未配置或版本已变化，请重新预览")
    return row


def preview_managed(
    url: str,
    pair_id: str,
    account_address: str,
    config_revision: str,
    target_notional: str,
    request_id: str,
    wallet_version: int,
    session: SessionInfo,
    leg_indices: list[int] | None = None,
    max_slippage_bps: int = 5,
) -> dict:
    max_slippage_bps = adjustments._slippage(max_slippage_bps)
    request_id = _request_id(request_id)
    if type(wallet_version) is not int or wallet_version < 1:
        raise wallets.WalletError(422, "托管钱包版本非法")
    target = adjustments._decimal(target_notional, "每腿目标名义")
    selected = adjustments._leg_indices(leg_indices)
    descriptor, account = adjustments._resolve(url, pair_id, account_address, config_revision, session.account_id)
    payload_hash = _payload_hash(descriptor, account, target, config_revision, wallet_version, selected, max_slippage_bps)
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            existing = _matching_existing(
                _request_row(cursor, request_id, owner_account_id=session.account_id), session, payload_hash
            )
            if existing is not None:
                return _view(existing)
            _active_wallet(cursor, descriptor, account, wallet_version)
            if wallets._active_execution(cursor, descriptor["network"], pair_id, account):
                raise wallets.WalletError(409, "存在执行中或待核对的调仓，请完成核对后再预览")
    if selected == [0, 1]:
        report = adjustments.preview(url, pair_id, account, config_revision, target_notional, session.account_id, max_slippage_bps=max_slippage_bps)
    else:
        report = adjustments.preview(
            url, pair_id, account, config_revision, target_notional, session.account_id, selected, max_slippage_bps=max_slippage_bps,
        )
    preview_hash = _preview_hash(report)
    report.update({
        "request_id": request_id,
        "wallet_version": wallet_version,
        "managed_status": "previewed",
    })
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            wallets._lock_scope(cursor, descriptor["network"], pair_id, account)
            existing = _matching_existing(
                _request_row(cursor, request_id, lock=True, owner_account_id=session.account_id), session, payload_hash
            )
            if existing is not None:
                return _view(existing)
            wallet = _active_wallet(cursor, descriptor, account, wallet_version)
            if wallets._active_execution(cursor, descriptor["network"], pair_id, account):
                raise wallets.WalletError(409, "存在执行中或待核对的调仓，请完成核对后再预览")
            cursor.execute(
                """
                INSERT INTO hyperliquid_execution_requests (
                    request_id, actor, network, pair_id, account_address, config_revision,
                    target_notional, request_payload_hash, preview_hash, wallet_address,
                    wallet_version, status, result, owner_account_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'previewed', %s, %s
                )
                RETURNING *
                """,
                (
                    request_id, wallets._actor(session), descriptor["network"], pair_id, account,
                    config_revision, adjustments._decimal_arg(target), payload_hash, preview_hash,
                    wallet["wallet_address"], wallet_version, Json(report), session.account_id,
                ),
            )
            return _view(dict(cursor.fetchone()))


def _finish(
    url: str,
    request_id: str,
    status: str,
    error_code: str | None,
    detail: str | None,
    result: dict | None = None,
    expected_status: str | None = None,
) -> dict:
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            row = _request_row(cursor, request_id, lock=True)
            if row is None:
                raise wallets.WalletError(404, "调仓请求不存在")
            if expected_status is not None and row["status"] != expected_status:
                if row["status"] == status and row["error_code"] == error_code:
                    return _view(row)
                raise wallets.WalletError(409, "调仓请求状态已变化，请重新读取")
            if result is None:
                result = dict(row["result"] or {})
            if detail:
                diagnostics = list(result.get("diagnostics") or [])
                diagnostics.append(detail)
                result["diagnostics"] = diagnostics
            if status in {"failed", "unknown"}:
                result["status"] = "incomplete" if status == "failed" else "unknown"
            action_id = result.get("action_id") or row["action_id"]
            result.update({
                "request_id": request_id,
                "wallet_version": row["wallet_version"],
                "managed_status": status,
                "action_id": action_id,
                "error_code": error_code,
            })
            cursor.execute(
                """
                UPDATE hyperliquid_execution_requests SET
                    status = %s, action_id = %s, result = %s, error_code = %s,
                    updated_at = now(), finished_at = now()
                WHERE request_id = %s RETURNING *
                """,
                (status, action_id, Json(result), error_code, request_id),
            )
            finished = dict(cursor.fetchone())
            wallets._event(
                cursor, "execute_finished", finished, row["actor"], request_id, status
            )
            return _view(finished)


def execute_managed(
    url: str,
    pair_id: str,
    account_address: str,
    config_revision: str,
    target_notional: str,
    confirmation: str,
    request_id: str,
    wallet_version: int,
    session: SessionInfo,
    leg_indices: list[int] | None = None,
    max_slippage_bps: int = 5,
) -> tuple[dict, int]:
    max_slippage_bps = adjustments._slippage(max_slippage_bps)
    request_id = _request_id(request_id)
    target = adjustments._decimal(target_notional, "每腿目标名义")
    if confirmation.strip() != adjustments._decimal_arg(target):
        raise wallets.WalletError(422, "确认金额必须与每腿目标名义完全一致")
    selected = adjustments._leg_indices(leg_indices)
    descriptor, account = adjustments._resolve(url, pair_id, account_address, config_revision, session.account_id)
    payload_hash = _payload_hash(descriptor, account, target, config_revision, wallet_version, selected, max_slippage_bps)
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            wallets._lock_scope(cursor, descriptor["network"], pair_id, account)
            row = _matching_existing(
                _request_row(cursor, request_id, lock=True, owner_account_id=session.account_id), session, payload_hash
            )
            if row is None:
                raise wallets.WalletError(409, "找不到对应的托管调仓预览，请重新预览")
            if row["network"] != descriptor["network"] or row["pair_id"] != pair_id or row["account_address"] != account:
                raise wallets.WalletError(409, "调仓请求身份不匹配")
            if row["status"] != "previewed":
                return _view(row), 202 if row["status"] == "running" else 200
            wallet = _active_wallet(cursor, descriptor, account, wallet_version)
            if wallets._active_execution(cursor, descriptor["network"], pair_id, account):
                raise wallets.WalletError(409, "该账户已有执行中或待核对的调仓")
            cursor.execute(
                """
                UPDATE hyperliquid_execution_requests SET
                    status = 'running', started_at = now(), updated_at = now()
                WHERE request_id = %s AND status = 'previewed' RETURNING *
                """,
                (request_id,),
            )
            row = dict(cursor.fetchone())
            wallets._event(cursor, "execute_requested", row, row["actor"], request_id, "running")
    if os.environ.get("HL_APP_TRADING_ENABLED", "").strip().lower() != "true":
        return _finish(url, request_id, "failed", "trading_disabled", "真实调仓未开启，未发送订单"), 200
    try:
        authorization.verify_authorization(
            descriptor["network"], account, wallet["wallet_address"]
        )
        private_key = wallet_crypto.decrypt_storage(
            wallet["encrypted_private_key"], descriptor["network"], pair_id, account,
            wallet["wallet_address"], wallet_version,
        )
        argv = adjustments._base_argv(descriptor, account, target, "adjust", selected, max_slippage_bps)
        argv.extend([
            "--secret-fd", "0", "--state-dir", adjustments._state_dir(),
            "--request-id", request_id, "--execute",
        ])
        if not hasattr(os, "memfd_create"):
            raise wallet_crypto.WalletCryptoError(503, "当前系统不支持安全的托管私钥传递")
        fd = os.memfd_create("hl-managed-adjustment", flags=os.MFD_CLOEXEC)
        try:
            encoded = private_key.encode("ascii")
            view = memoryview(encoded)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.lseek(fd, 0, os.SEEK_SET)
            argv[argv.index("--secret-fd") + 1] = str(fd)
            if selected == [0, 1]:
                report = adjustments._invoke(
                    argv, descriptor, account, target, execute=True, pass_fds=(fd,),
                )
            else:
                report = adjustments._invoke(
                    argv, descriptor, account, target, execute=True, pass_fds=(fd,), leg_indices=selected,
                )
        finally:
            os.close(fd)
            pairs_service.clear_runtime_cache()
        if report.get("request_id") != request_id:
            raise adjustments.AdjustmentError(503, "调仓结果的 request_id 不匹配")
        status = {
            "succeeded": "succeeded",
            "incomplete": "incomplete",
            "unknown": "unknown",
        }.get(report.get("status"), "unknown")
        return _finish(url, request_id, status, None, None, report), 200
    except (authorization.AuthorizationError, wallet_crypto.WalletCryptoError) as exc:
        return _finish(url, request_id, "failed", "preflight_failed", f"未发送订单：{exc.detail}"), 200
    except adjustments.AdjustmentError as exc:
        if exc.status_code < 500:
            return _finish(url, request_id, "failed", "execution_refused", f"未发送订单：{exc.detail}"), 200
        return _finish(url, request_id, "unknown", "execution_unresolved", exc.detail), 200
    except Exception:
        return _finish(
            url, request_id, "unknown", "execution_unresolved",
            "调仓执行异常；订单结果未知，请按 request_id 和回执核对，禁止直接重试",
        ), 200


def get_managed_execution(url: str, pair_id: str, request_id: str, owner_account_id: int | None = None) -> dict:
    request_id = _request_id(request_id)
    _revision, descriptors = pairs_service.load_pairs()
    descriptor = next((item for item in descriptors if item["id"] == pair_id), None)
    if descriptor is None:
        raise wallets.WalletError(404, "未知或停用的 pair")
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            row = _request_row(cursor, request_id, owner_account_id=owner_account_id)
            if row is None or row["pair_id"] != pair_id or row["network"] != descriptor["network"]:
                raise wallets.WalletError(404, "调仓请求不存在")
            return _view(row)


def resolve_not_sent(url: str, pair_id: str, request_id: str, owner_account_id: int | None = None) -> dict:
    request_id = _request_id(request_id)
    _revision, descriptors = pairs_service.load_pairs()
    descriptor = next((item for item in descriptors if item["id"] == pair_id), None)
    if descriptor is None:
        raise wallets.WalletError(404, "未知或停用的 pair")
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            row = _request_row(cursor, request_id, lock=True, owner_account_id=owner_account_id)
            if row is None or row["pair_id"] != pair_id or row["network"] != descriptor["network"]:
                raise wallets.WalletError(404, "调仓请求不存在")
            if row["status"] == "failed" and row["error_code"] == "not_sent_confirmed":
                return _view(row)
            if row["status"] != "unknown":
                raise wallets.WalletError(409, "只有待核对请求可以执行未发送核对")
            _require_pre_send_unknown(row)
            if _receipt_exists(pair_id, row["account_address"], request_id):
                raise wallets.WalletError(409, "已找到该请求的执行回执，不能按未发送解除；请按回执人工核对")
    return _finish(
        url,
        request_id,
        "failed",
        "not_sent_confirmed",
        "已核对：未生成 action_id、cloid、nonce 或订单 ID，且不存在该 request_id 的执行回执；按执行器先保存回执、后发送订单的顺序，本次未发送订单，已解除阻塞",
        expected_status="unknown",
    )


def mark_interrupted_requests(url: str) -> None:
    with connect(url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT request_id FROM hyperliquid_execution_requests WHERE status = 'running'")
            request_ids = [str(row[0]) for row in cursor.fetchall()]
    for request_id in request_ids:
        _finish(
            url, request_id, "unknown", "process_interrupted",
            "服务进程曾中断，订单结果未知；请核对执行回执和交易所记录，禁止直接重试",
        )
