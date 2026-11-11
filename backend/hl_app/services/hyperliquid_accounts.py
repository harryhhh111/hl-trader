from __future__ import annotations

import re

from psycopg2 import errors
from psycopg2.extras import RealDictCursor

from ..db import connect

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
ZERO_ADDRESS = "0x" + "0" * 40


class PairAccountError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def normalize_address(value: str) -> str:
    address = value.strip().lower()
    if not ADDRESS_RE.fullmatch(address) or address == ZERO_ADDRESS:
        raise PairAccountError(422, "账户地址必须是合法的非零 EVM 地址")
    return address


def _row(row: dict) -> dict:
    return {
        "id": row["id"],
        "label": row["label"],
        "address": row["address"],
        "is_active": row["is_active"],
        "source": "database",
        "created_at": row["created_at"].isoformat(),
    }


def _rows(cursor, pair_id: str, owner_account_id: int | None) -> list[dict]:
    ownership = "" if owner_account_id is None else " AND owner_account_id = %s"
    params = (pair_id,) if owner_account_id is None else (pair_id, owner_account_id)
    cursor.execute(
        f"""
        SELECT id, label, address, is_active, created_at
        FROM hyperliquid_pair_accounts WHERE pair_id = %s{ownership} ORDER BY id
        """,
        params,
    )
    return [_row(dict(row)) for row in cursor.fetchall()]


def _lock_pair(cursor, pair_id: str) -> None:
    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (pair_id,))


def list_accounts(url: str, descriptor: dict, owner_account_id: int | None = None) -> dict:
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            accounts = _rows(cursor, descriptor["id"], owner_account_id)
    if owner_account_id is None and not accounts and descriptor["account_address"] is not None:
        accounts = [{
            "id": None,
            "label": "默认账户",
            "address": descriptor["account_address"],
            "is_active": True,
            "source": "config",
            "created_at": None,
        }]
    return {"pair_id": descriptor["id"], "accounts": accounts}


def add_account(url: str, descriptor: dict, label: str, address: str, owner_account_id: int | None = None) -> dict:
    label = label.strip()
    if not label:
        raise PairAccountError(422, "账户名称不能为空")
    address = normalize_address(address)
    pair_id = descriptor["id"]
    try:
        with connect(url, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cursor:
                _lock_pair(cursor, pair_id)
                existing = _rows(cursor, pair_id, owner_account_id)
                if any(item["address"] == address for item in existing):
                    raise PairAccountError(409, "该账户地址已添加")
                if owner_account_id is None and not existing and descriptor["account_address"] == address:
                    raise PairAccountError(409, "该账户地址已是默认账户")
                cursor.execute(
                    "UPDATE hyperliquid_pair_accounts SET is_active = false, updated_at = now() WHERE pair_id = %s AND owner_account_id IS NOT DISTINCT FROM %s",
                    (pair_id, owner_account_id),
                )
                if owner_account_id is None and not existing and descriptor["account_address"] is not None:
                    cursor.execute(
                        """
                        INSERT INTO hyperliquid_pair_accounts (pair_id, label, address, is_active)
                        VALUES (%s, '默认账户', %s, false)
                        """,
                        (pair_id, descriptor["account_address"]),
                    )
                cursor.execute(
                    """
                    INSERT INTO hyperliquid_pair_accounts (pair_id, label, address, is_active, owner_account_id)
                    VALUES (%s, %s, %s, true, %s)
                    RETURNING id, label, address, is_active, created_at
                    """,
                    (pair_id, label, address, owner_account_id),
                )
                account = _row(dict(cursor.fetchone()))
    except errors.UniqueViolation as exc:
        raise PairAccountError(409, "该账户地址已添加") from exc
    return account


def activate_account(url: str, descriptor: dict, account_id: int, owner_account_id: int | None = None) -> dict:
    pair_id = descriptor["id"]
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            _lock_pair(cursor, pair_id)
            cursor.execute(
                """
                UPDATE hyperliquid_pair_accounts SET is_active = false, updated_at = now()
                WHERE pair_id = %s AND owner_account_id IS NOT DISTINCT FROM %s
                """,
                (pair_id, owner_account_id),
            )
            cursor.execute(
                """
                UPDATE hyperliquid_pair_accounts SET is_active = true, updated_at = now()
                WHERE pair_id = %s AND id = %s AND owner_account_id IS NOT DISTINCT FROM %s
                RETURNING id, label, address, is_active, created_at
                """,
                (pair_id, account_id, owner_account_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise PairAccountError(404, "账户不存在或不属于当前用户")
            account = _row(dict(row))
    return account


def apply_active_accounts(url: str, descriptors: list[dict], owner_account_id: int | None = None) -> list[dict]:
    if not descriptors:
        return descriptors
    pair_ids = [item["id"] for item in descriptors]
    ownership = "" if owner_account_id is None else " AND owner_account_id = %s"
    params = (pair_ids,) if owner_account_id is None else (pair_ids, owner_account_id)
    with connect(url, cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT pair_id, address FROM hyperliquid_pair_accounts
                WHERE pair_id = ANY(%s) AND is_active = true{ownership}
                """,
                params,
            )
            active = {row["pair_id"]: row["address"] for row in cursor.fetchall()}
    return [
        {**item, "account_address": active.get(item["id"], item["account_address"] if owner_account_id is None else None)}
        for item in descriptors
    ]
