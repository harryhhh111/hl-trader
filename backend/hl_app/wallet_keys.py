from __future__ import annotations

import argparse
import base64
import os
import stat
from pathlib import Path
from typing import Sequence

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg2.extras import RealDictCursor

from .config import ConfigError, database_url
from .db import connect
from .services.hyperliquid_wallet_crypto import STORAGE_PREFIX, storage_aad


def _key(path: str) -> bytes:
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o077:
        raise ConfigError(f"密钥文件权限必须禁止 group/other 访问: {path}")
    encoded = Path(path).read_bytes().strip()
    key = base64.b64decode(encoded, validate=True)
    if len(key) != 32 or base64.b64encode(key) != encoded:
        raise ConfigError(f"数据密钥必须是 Base64 编码的 32 字节随机数据: {path}")
    return key


def rotate(old_key_file: str, new_key_file: str, old_key_id: str, new_key_id: str, apply: bool) -> int:
    old_key = _key(old_key_file)
    new_key = _key(new_key_file)
    if old_key == new_key or old_key_id == new_key_id:
        raise ConfigError("新旧数据密钥及 key_id 必须不同")
    with connect(database_url("migration"), cursor_factory=RealDictCursor) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM hyperliquid_api_wallets WHERE status = 'active' FOR UPDATE")
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                parts = row["encrypted_private_key"].removeprefix(STORAGE_PREFIX).split(":")
                if len(parts) != 3 or parts[0] != old_key_id:
                    raise ConfigError("存在非当前旧密钥的 active 密文，已停止轮换")
                nonce = base64.b64decode(parts[1], validate=True)
                ciphertext = base64.b64decode(parts[2], validate=True)
                aad = storage_aad(
                    row["network"], row["pair_id"], row["account_address"],
                    row["wallet_address"], row["wallet_version"],
                )
                plaintext = AESGCM(old_key).decrypt(nonce, ciphertext, aad)
                new_nonce = os.urandom(12)
                new_ciphertext = AESGCM(new_key).encrypt(new_nonce, plaintext, aad)
                encoded = STORAGE_PREFIX + ":".join((
                    new_key_id,
                    base64.b64encode(new_nonce).decode("ascii"),
                    base64.b64encode(new_ciphertext).decode("ascii"),
                ))
                if apply:
                    cursor.execute(
                        """
                        UPDATE hyperliquid_api_wallets SET encrypted_private_key = %s, data_key_id = %s
                        WHERE network = %s AND pair_id = %s AND account_address = %s AND wallet_version = %s
                        """,
                        (
                            encoded, new_key_id, row["network"], row["pair_id"],
                            row["account_address"], row["wallet_version"],
                        ),
                    )
            if not apply:
                conn.rollback()
    print(f"mode={'apply' if apply else 'dry-run'} active_wallets={len(rows)} new_key_id={new_key_id}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hl_app.wallet_keys")
    subparsers = parser.add_subparsers(dest="command", required=True)
    rotate_parser = subparsers.add_parser("rotate")
    rotate_parser.add_argument("--old-key-file", required=True)
    rotate_parser.add_argument("--new-key-file", required=True)
    rotate_parser.add_argument("--old-key-id", required=True)
    rotate_parser.add_argument("--new-key-id", required=True)
    rotate_parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "rotate":
        return rotate(args.old_key_file, args.new_key_file, args.old_key_id, args.new_key_id, args.apply)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(f"密钥轮换失败，未提交部分修改: {exc}") from exc
