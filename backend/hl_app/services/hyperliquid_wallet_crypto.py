from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PURPOSE = "hyperliquid-api-wallet-v1"
STORAGE_PREFIX = "ENC:v1:"
KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
PRIVATE_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
AAD_FIELDS = {
    "purpose",
    "challenge_id",
    "actor_binding",
    "key_id",
    "network",
    "operation",
    "pair_id",
    "account_address",
    "wallet_version",
    "ts",
}


class WalletCryptoError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _secret_file(variable: str) -> bytes:
    value = os.environ.get(variable, "").strip()
    if not value or not Path(value).is_absolute():
        raise WalletCryptoError(503, "托管钱包加密未配置")
    try:
        info = os.stat(value, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o077:
            raise WalletCryptoError(503, "托管钱包密钥文件权限不安全")
        return Path(value).read_bytes()
    except WalletCryptoError:
        raise
    except OSError as exc:
        raise WalletCryptoError(503, "托管钱包密钥不可用") from exc


def _data_key() -> tuple[str, bytes]:
    key_id = os.environ.get("HL_APP_DATA_ENCRYPTION_KEY_ID", "").strip()
    if not KEY_ID_RE.fullmatch(key_id):
        raise WalletCryptoError(503, "托管钱包加密未配置")
    encoded = _secret_file("HL_APP_DATA_ENCRYPTION_KEY_FILE").strip()
    try:
        key = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise WalletCryptoError(503, "托管钱包数据密钥格式非法") from exc
    if len(key) != 32 or base64.b64encode(key) != encoded:
        raise WalletCryptoError(503, "托管钱包数据密钥格式非法")
    return key_id, key


def _rsa_private_key() -> rsa.RSAPrivateKey:
    try:
        key = serialization.load_pem_private_key(
            _secret_file("HL_APP_TRANSPORT_RSA_PRIVATE_KEY_FILE"), password=None
        )
    except WalletCryptoError:
        raise
    except Exception as exc:
        raise WalletCryptoError(503, "托管钱包传输密钥不可用") from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 3072:
        raise WalletCryptoError(503, "托管钱包传输密钥格式非法")
    return key


def transport_public_key() -> dict[str, str]:
    key = _rsa_private_key().public_key()
    pem = key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    key_id = hashlib.sha256(pem.encode("ascii")).hexdigest()[:32]
    return {"key_id": key_id, "public_key": pem, "algorithm": "RSA-OAEP-3072"}


def _b64url(value: object, minimum: int = 1) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 16384:
        raise WalletCryptoError(422, "托管钱包加密请求非法")
    try:
        raw = base64.b64decode(
            value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except Exception as exc:
        raise WalletCryptoError(422, "托管钱包加密请求非法") from exc
    if len(raw) < minimum:
        raise WalletCryptoError(422, "托管钱包加密请求非法")
    return raw


def decrypt_transport(payload: dict, expected: dict) -> str:
    if not isinstance(payload, dict) or set(payload) != {
        "wrapped_key",
        "iv",
        "ciphertext",
        "aad",
        "key_id",
        "challenge_id",
        "ts",
    }:
        raise WalletCryptoError(422, "托管钱包加密请求非法")
    if payload["key_id"] != transport_public_key()["key_id"]:
        raise WalletCryptoError(422, "托管钱包传输密钥已变化，请重新操作")
    aad_bytes = _b64url(payload["aad"])
    try:
        aad = json.loads(aad_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WalletCryptoError(422, "托管钱包加密请求非法") from exc
    if not isinstance(aad, dict) or set(aad) != AAD_FIELDS:
        raise WalletCryptoError(422, "托管钱包加密请求非法")
    expected_aad = {"purpose": PURPOSE, **expected}
    if aad != expected_aad or payload["ts"] != aad["ts"]:
        raise WalletCryptoError(422, "托管钱包请求身份或版本不匹配")
    if not isinstance(aad["ts"], int) or isinstance(aad["ts"], bool):
        raise WalletCryptoError(422, "托管钱包请求时间非法")
    elapsed = datetime.now(timezone.utc).timestamp() - aad["ts"]
    if elapsed > 300 or elapsed < -60:
        raise WalletCryptoError(422, "托管钱包请求已过期，请重新操作")
    wrapped = _b64url(payload["wrapped_key"])
    iv = _b64url(payload["iv"])
    ciphertext = _b64url(payload["ciphertext"], minimum=17)
    if len(iv) != 12:
        raise WalletCryptoError(422, "托管钱包加密请求非法")
    try:
        data_key = _rsa_private_key().decrypt(
            wrapped,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        plaintext = AESGCM(data_key).decrypt(iv, ciphertext, aad_bytes)
        body = json.loads(plaintext)
    except Exception as exc:
        raise WalletCryptoError(422, "托管钱包加密校验失败") from exc
    if not isinstance(body, dict) or set(body) != {"private_key"}:
        raise WalletCryptoError(422, "托管钱包私钥格式非法")
    private_key = body["private_key"]
    if not isinstance(private_key, str):
        raise WalletCryptoError(422, "托管钱包私钥格式非法")
    private_key = private_key.strip().lower()
    if not PRIVATE_KEY_RE.fullmatch(private_key) or private_key == "0x" + "0" * 64:
        raise WalletCryptoError(422, "API Wallet 私钥必须是 64 位非零十六进制数据")
    return private_key


def storage_aad(
    network: str,
    pair_id: str,
    account_address: str,
    wallet_address: str,
    wallet_version: int,
) -> bytes:
    return (
        f"{PURPOSE}|{network}|{pair_id}|{account_address}|"
        f"{wallet_address}|{wallet_version}"
    ).encode("ascii")


def encrypt_storage(
    private_key: str,
    network: str,
    pair_id: str,
    account_address: str,
    wallet_address: str,
    wallet_version: int,
) -> str:
    key_id, key = _data_key()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        private_key.encode("ascii"),
        storage_aad(network, pair_id, account_address, wallet_address, wallet_version),
    )
    return STORAGE_PREFIX + ":".join(
        (key_id, base64.b64encode(nonce).decode("ascii"), base64.b64encode(ciphertext).decode("ascii"))
    )


def decrypt_storage(
    value: str,
    network: str,
    pair_id: str,
    account_address: str,
    wallet_address: str,
    wallet_version: int,
) -> str:
    key_id, key = _data_key()
    if not isinstance(value, str) or not value.startswith(STORAGE_PREFIX):
        raise WalletCryptoError(503, "托管钱包密文格式非法")
    parts = value.removeprefix(STORAGE_PREFIX).split(":")
    if len(parts) != 3 or parts[0] != key_id:
        raise WalletCryptoError(503, "托管钱包数据密钥版本不匹配")
    try:
        nonce = base64.b64decode(parts[1], validate=True)
        ciphertext = base64.b64decode(parts[2], validate=True)
        plaintext = AESGCM(key).decrypt(
            nonce,
            ciphertext,
            storage_aad(network, pair_id, account_address, wallet_address, wallet_version),
        ).decode("ascii")
    except Exception as exc:
        raise WalletCryptoError(503, "托管钱包密文校验失败") from exc
    if len(nonce) != 12 or not PRIVATE_KEY_RE.fullmatch(plaintext):
        raise WalletCryptoError(503, "托管钱包密文内容非法")
    return plaintext
