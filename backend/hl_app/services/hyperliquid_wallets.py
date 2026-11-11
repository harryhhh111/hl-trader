from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess

from psycopg2.extras import RealDictCursor

from ..db import connect
from ..auth import SessionInfo
from . import hyperliquid_accounts as accounts_service
from . import hyperliquid_pairs as pairs_service
from . import hyperliquid_wallet_authorization as authorization
from . import hyperliquid_wallet_crypto as wallet_crypto
from .process import HyperliquidUnavailable, communicate_bounded


class WalletError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _actor(session: SessionInfo) -> str:
    return session.email.lower() if session.email else f"session:{session.session_sha256}"


def _actor_binding(session: SessionInfo) -> str:
    return hashlib.sha256(f"{_actor(session)}|{session.session_sha256}".encode("utf-8")).hexdigest()


def _scope_key(network: str, pair_id: str, account_address: str) -> str:
    return f"{network}|{pair_id}|{account_address}"


def _lock_scope(cursor, network: str, pair_id: str, account_address: str) -> None:
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (_scope_key(network, pair_id, account_address),),
    )


def _resolve(
    url: str, pair_id: str, account_address: str, config_revision: str | None = None,
    owner_account_id: int | None = None,
) -> tuple[dict, str]:
    revision, descriptors = pairs_service.load_pairs()
    if config_revision is not None and revision != config_revision:
        raise WalletError(409, "pair 配置已变化，请刷新后重新操作")
    descriptor = next((item for item in descriptors if item["id"] == pair_id), None)
    if descriptor is None:
        raise WalletError(404, "未知或停用的 pair")
    active = accounts_service.apply_active_accounts(url, [descriptor], owner_account_id)[0]
    account = accounts_service.normalize_address(account_address)
    if active["account_address"] != account:
        raise WalletError(409, "当前账户已变化，请刷新后重新操作")
    return active, account


def _wallet_row(cursor, network: str, pair_id: str, account: str, lock: bool = False) -> dict | None:
    suffix = " FOR UPDATE" if lock else ""
    cursor.execute(
        f"""
        SELECT * FROM hyperliquid_api_wallets
        WHERE network = %s AND pair_id = %s AND account_address = %s{suffix}
        """,
        (network, pair_id, account),
    )
    row = cursor.fetchone()
    return dict(row) if row is not None else None


def _active_execution(cursor, network: str, pair_id: str, account: str) -> dict | None:
    cursor.execute(
        """
        SELECT request_id, status FROM hyperliquid_execution_requests
        WHERE network = %s AND pair_id = %s AND account_address = %s
          AND status IN ('running', 'unknown')
        """,
        (network, pair_id, account),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {"request_id": str(row["request_id"]), "status": row["status"]}


def _metadata(row: dict | None, active_execution: dict | None = None) -> dict:
    if row is None:
        return {
            "configured": False,
            "wallet_address": None,
            "wallet_version": 0,
            "authorization_checked_at": None,
            "created_at": None,
            "updated_at": None,
            "active_execution": active_execution,
        }
    active = row["status"] == "active"
    return {
        "configured": active,
        "wallet_address": row["wallet_address"] if active else None,
        "wallet_version": row["wallet_version"],
        "authorization_checked_at": row["authorization_checked_at"].isoformat()
        if active and row["authorization_checked_at"] is not None else None,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
        "active_execution": active_execution,
    }


def get_wallet(url: str, pair_id: str, account_address: str, owner_account_id: int | None = None) -> dict:
    descriptor, account = _resolve(url, pair_id, account_address, owner_account_id=owner_account_id)
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            row = _wallet_row(cursor, descriptor["network"], pair_id, account)
            active = _active_execution(cursor, descriptor["network"], pair_id, account)
            return {"network": descriptor["network"], "pair_id": pair_id,
                    "account_address": account, **_metadata(row, active)}


def _challenge_hash(challenge_id: str) -> str:
    if not isinstance(challenge_id, str) or not 20 <= len(challenge_id) <= 128:
        raise WalletError(422, "一次性操作凭证非法")
    return hashlib.sha256(challenge_id.encode("ascii", "strict")).hexdigest()


def _consume_challenge(
    cursor,
    challenge_id: str,
    session: SessionInfo,
    descriptor: dict,
    account: str,
    operation: str,
    wallet_version: int,
) -> None:
    cursor.execute(
        """
        UPDATE hyperliquid_api_wallet_challenges SET used_at = now()
        WHERE challenge_hash = %s AND used_at IS NULL AND expires_at > now()
        RETURNING actor_binding, network, pair_id, account_address, operation, wallet_version
        """,
        (_challenge_hash(challenge_id),),
    )
    row = cursor.fetchone()
    expected = (
        _actor_binding(session), descriptor["network"], descriptor["id"], account,
        operation, wallet_version,
    )
    actual = None if row is None else (
        row["actor_binding"], row["network"], row["pair_id"], row["account_address"],
        row["operation"], row["wallet_version"],
    )
    if actual != expected:
        raise WalletError(409, "一次性操作凭证无效、已使用或版本已变化")


def _peek_challenge(
    url: str,
    challenge_id: str,
    session: SessionInfo,
    descriptor: dict,
    account: str,
) -> tuple[str, int]:
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM hyperliquid_api_wallet_challenges
                WHERE challenge_hash = %s AND used_at IS NULL AND expires_at > now()
                """,
                (_challenge_hash(challenge_id),),
            )
            row = cursor.fetchone()
            if row is None:
                raise WalletError(409, "一次性操作凭证无效或已过期")
            expected = {
                "actor_binding": _actor_binding(session),
                "network": descriptor["network"],
                "pair_id": descriptor["id"],
                "account_address": account,
            }
            if any(row[key] != value for key, value in expected.items()):
                raise WalletError(409, "一次性操作凭证与当前操作不匹配")
            return row["operation"], row["wallet_version"]


def issue_challenge(
    url: str,
    pair_id: str,
    account_address: str,
    config_revision: str,
    operation: str,
    session: SessionInfo,
) -> dict:
    if operation not in {"save", "rotate", "delete"}:
        raise WalletError(422, "托管钱包操作类型非法")
    descriptor, account = _resolve(url, pair_id, account_address, config_revision, session.account_id)
    challenge_id = secrets.token_urlsafe(32)
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            _lock_scope(cursor, descriptor["network"], pair_id, account)
            row = _wallet_row(cursor, descriptor["network"], pair_id, account, lock=True)
            version = row["wallet_version"] if row else 0
            configured = bool(row and row["status"] == "active")
            if operation == "save" and configured:
                raise WalletError(409, "已配置托管钱包，请使用更换操作")
            if operation in {"rotate", "delete"} and not configured:
                raise WalletError(409, "当前账户尚未配置托管钱包")
            if operation in {"rotate", "delete"} and _active_execution(
                cursor, descriptor["network"], pair_id, account
            ):
                raise WalletError(409, "存在执行中或待核对的调仓，暂不能变更钱包")
            cursor.execute(
                """
                INSERT INTO hyperliquid_api_wallet_challenges (
                    challenge_hash, actor_binding, network, pair_id, account_address,
                    operation, wallet_version, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now() + interval '5 minutes')
                """,
                (
                    _challenge_hash(challenge_id), _actor_binding(session), descriptor["network"],
                    pair_id, account, operation, version,
                ),
            )
    return {
        "challenge_id": challenge_id,
        "actor_binding": _actor_binding(session),
        "operation": operation,
        "wallet_version": version,
        **wallet_crypto.transport_public_key(),
    }


def derive_wallet_address(private_key: str) -> str:
    if not hasattr(os, "memfd_create"):
        raise WalletError(503, "当前系统不支持安全的托管私钥传递")
    binary = os.environ.get("HL_APP_TRADER_BIN", "").strip()
    if not binary:
        raise WalletError(503, "托管钱包地址校验器未配置")
    fd = os.memfd_create("hl-api-wallet", flags=os.MFD_CLOEXEC)
    try:
        encoded = private_key.encode("ascii")
        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            proc = subprocess.Popen(
                [binary, "api-wallet-address", "--secret-fd", str(fd), "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(fd,),
            )
        except OSError as exc:
            raise WalletError(503, "无法启动托管钱包地址校验器") from exc
        try:
            stdout, _stderr = communicate_bounded(proc, 5)
        except HyperliquidUnavailable as exc:
            raise WalletError(503, "托管钱包地址校验失败") from exc
        if proc.returncode != 0:
            raise WalletError(422, "API Wallet 私钥不是合法的 secp256k1 密钥")
    finally:
        os.close(fd)
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise WalletError(503, "托管钱包地址校验器响应非法") from exc
    if not isinstance(result, dict) or set(result) != {"schema_version", "address"} or result["schema_version"] != 1:
        raise WalletError(503, "托管钱包地址校验器响应非法")
    return accounts_service.normalize_address(result["address"])


def save_wallet(
    url: str,
    pair_id: str,
    account_address: str,
    config_revision: str,
    payload: dict,
    session: SessionInfo,
) -> dict:
    descriptor, account = _resolve(url, pair_id, account_address, config_revision, session.account_id)
    if not isinstance(payload, dict) or not isinstance(payload.get("challenge_id"), str):
        raise WalletError(422, "托管钱包加密请求非法")
    challenge_id = payload["challenge_id"]
    operation, version = _peek_challenge(url, challenge_id, session, descriptor, account)
    if operation == "delete":
        raise WalletError(422, "删除操作不能提交私钥")
    expected = {
        "challenge_id": challenge_id,
        "actor_binding": _actor_binding(session),
        "key_id": wallet_crypto.transport_public_key()["key_id"],
        "network": descriptor["network"],
        "operation": operation,
        "pair_id": pair_id,
        "account_address": account,
        "wallet_version": version,
        "ts": payload.get("ts"),
    }
    private_key = wallet_crypto.decrypt_transport(payload, expected)
    wallet_address = derive_wallet_address(private_key)
    if wallet_address == account:
        raise WalletError(422, "禁止保存原交易账户私钥")
    master = authorization.verify_authorization(descriptor["network"], account, wallet_address)
    new_version = version + 1
    encrypted = wallet_crypto.encrypt_storage(
        private_key, descriptor["network"], pair_id, account, wallet_address, new_version
    )
    actor = _actor(session)
    try:
        with connect(url, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cursor:
                _lock_scope(cursor, descriptor["network"], pair_id, account)
                previous = _wallet_row(cursor, descriptor["network"], pair_id, account, lock=True)
                current_version = previous["wallet_version"] if previous else 0
                if current_version != version:
                    raise WalletError(409, "托管钱包版本已变化，请重新操作")
                if operation == "rotate" and _active_execution(
                    cursor, descriptor["network"], pair_id, account
                ):
                    raise WalletError(409, "存在执行中或待核对的调仓，暂不能更换钱包")
                cursor.execute(
                    """
                    SELECT 1 FROM hyperliquid_api_wallets
                    WHERE network = %s AND wallet_address = %s AND status = 'active'
                      AND (pair_id, account_address) <> (%s, %s)
                    """,
                    (descriptor["network"], wallet_address, pair_id, account),
                )
                if cursor.fetchone() is not None:
                    raise WalletError(409, "该 API Wallet 已被其他交易账户使用")
                cursor.execute(
                    "SELECT 1 FROM hyperliquid_api_wallet_retired_addresses WHERE network = %s AND wallet_address = %s",
                    (descriptor["network"], wallet_address),
                )
                if cursor.fetchone() is not None:
                    raise WalletError(409, "该 API Wallet 地址已淘汰，不能重新使用")
                _consume_challenge(
                    cursor, challenge_id, session, descriptor, account, operation, version
                )
                if previous and previous["status"] == "active":
                    cursor.execute(
                        """
                        INSERT INTO hyperliquid_api_wallet_retired_addresses (
                            network, wallet_address, pair_id, account_address, first_used_at
                        ) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                        """,
                        (
                            descriptor["network"], previous["wallet_address"], pair_id,
                            account, previous["created_at"],
                        ),
                    )
                cursor.execute(
                    """
                    INSERT INTO hyperliquid_api_wallets (
                        network, pair_id, account_address, wallet_address, wallet_version,
                        status, encrypted_private_key, encryption_version, data_key_id,
                        authorization_master, authorization_checked_at, created_by, updated_by, owner_account_id
                    ) VALUES (
                        %s, %s, %s, %s, %s, 'active', %s, 1, %s, %s, now(), %s, %s, %s
                    )
                    ON CONFLICT (network, pair_id, account_address) DO UPDATE SET
                        wallet_address = EXCLUDED.wallet_address,
                        wallet_version = EXCLUDED.wallet_version,
                        status = 'active', encrypted_private_key = EXCLUDED.encrypted_private_key,
                        encryption_version = 1, data_key_id = EXCLUDED.data_key_id,
                        authorization_master = EXCLUDED.authorization_master,
                        authorization_checked_at = now(), updated_by = EXCLUDED.updated_by,
                        updated_at = now()
                    RETURNING *
                    """,
                    (
                        descriptor["network"], pair_id, account, wallet_address, new_version,
                        encrypted, os.environ.get("HL_APP_DATA_ENCRYPTION_KEY_ID", "").strip(),
                        master, actor, actor, session.account_id,
                    ),
                )
                row = dict(cursor.fetchone())
                event = "rotate" if previous and previous["status"] == "active" else "save"
                _event(cursor, event, row, actor)
                return {"network": descriptor["network"], "pair_id": pair_id,
                        "account_address": account, **_metadata(row)}
    except WalletError:
        raise


def _event(cursor, event_type: str, row: dict, actor: str, request_id=None, outcome=None) -> None:
    cursor.execute(
        """
        INSERT INTO hyperliquid_api_wallet_events (
            event_type, network, pair_id, account_address, wallet_address, wallet_version,
            actor, request_id, action_id, outcome
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            event_type, row["network"], row["pair_id"], row["account_address"],
            row["wallet_address"], row["wallet_version"], actor, request_id,
            row.get("action_id"), outcome,
        ),
    )


def delete_wallet(
    url: str,
    pair_id: str,
    account_address: str,
    config_revision: str,
    challenge_id: str,
    wallet_address: str,
    wallet_version: int,
    session: SessionInfo,
) -> dict:
    descriptor, account = _resolve(url, pair_id, account_address, config_revision, session.account_id)
    confirmed_address = accounts_service.normalize_address(wallet_address)
    if type(wallet_version) is not int or wallet_version < 1:
        raise WalletError(422, "托管钱包版本非法")
    actor = _actor(session)
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            _lock_scope(cursor, descriptor["network"], pair_id, account)
            row = _wallet_row(cursor, descriptor["network"], pair_id, account, lock=True)
            if (
                row is None or row["status"] != "active"
                or row["wallet_version"] != wallet_version
                or row["wallet_address"] != confirmed_address
            ):
                raise WalletError(409, "托管钱包地址或版本已变化")
            if _active_execution(cursor, descriptor["network"], pair_id, account):
                raise WalletError(409, "存在执行中或待核对的调仓，暂不能删除钱包")
            _consume_challenge(
                cursor, challenge_id, session, descriptor, account, "delete", wallet_version
            )
            new_version = wallet_version + 1
            cursor.execute(
                """
                INSERT INTO hyperliquid_api_wallet_retired_addresses (
                    network, wallet_address, pair_id, account_address, first_used_at
                ) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                """,
                (descriptor["network"], confirmed_address, pair_id, account, row["created_at"]),
            )
            cursor.execute(
                """
                UPDATE hyperliquid_api_wallets SET
                    wallet_version = %s, status = 'deleted', encrypted_private_key = NULL,
                    authorization_master = NULL, authorization_checked_at = NULL,
                    updated_by = %s, updated_at = now()
                WHERE network = %s AND pair_id = %s AND account_address = %s
                RETURNING *
                """,
                (new_version, actor, descriptor["network"], pair_id, account),
            )
            deleted = dict(cursor.fetchone())
            _event(cursor, "delete", deleted, actor)
            return {"network": descriptor["network"], "pair_id": pair_id,
                    "account_address": account, **_metadata(deleted)}
