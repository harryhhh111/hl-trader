from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from hl_app.services import hyperliquid_wallet_crypto as crypto

PRIVATE_KEY = "0x0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
ACCOUNT = "0x" + "7" * 40
WALLET = "0x" + "1" * 40


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@pytest.fixture
def keys(monkeypatch, tmp_path):
    data_path = tmp_path / "data.key"
    data_path.write_bytes(base64.b64encode(os.urandom(32)))
    data_path.chmod(0o600)
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    rsa_path = tmp_path / "transport.pem"
    rsa_path.write_bytes(rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    rsa_path.chmod(0o600)
    monkeypatch.setenv("HL_APP_DATA_ENCRYPTION_KEY_FILE", str(data_path))
    monkeypatch.setenv("HL_APP_DATA_ENCRYPTION_KEY_ID", "test-dek-v1")
    monkeypatch.setenv("HL_APP_TRANSPORT_RSA_PRIVATE_KEY_FILE", str(rsa_path))
    return rsa_key


def transport_payload(rsa_key, ts: int | None = None) -> tuple[dict, dict]:
    public = crypto.transport_public_key()
    ts = ts or int(datetime.now(timezone.utc).timestamp())
    aad = {
        "purpose": crypto.PURPOSE,
        "challenge_id": "test-challenge-value",
        "actor_binding": "a" * 64,
        "key_id": public["key_id"],
        "network": "mainnet",
        "operation": "save",
        "pair_id": "nbis-crwv",
        "account_address": ACCOUNT,
        "wallet_version": 0,
        "ts": ts,
    }
    expected = dict(aad)
    data_key = os.urandom(32)
    iv = os.urandom(12)
    encoded_aad = json.dumps(aad, separators=(",", ":")).encode()
    ciphertext = AESGCM(data_key).encrypt(
        iv, json.dumps({"private_key": PRIVATE_KEY}, separators=(",", ":")).encode(), encoded_aad
    )
    wrapped = rsa_key.public_key().encrypt(
        data_key,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return {
        "wrapped_key": b64url(wrapped),
        "iv": b64url(iv),
        "ciphertext": b64url(ciphertext),
        "aad": b64url(encoded_aad),
        "key_id": public["key_id"],
        "challenge_id": "test-challenge-value",
        "ts": ts,
    }, expected


def test_transport_and_storage_roundtrip(keys) -> None:
    payload, expected = transport_payload(keys)
    assert crypto.decrypt_transport(payload, expected) == PRIVATE_KEY
    encrypted = crypto.encrypt_storage(PRIVATE_KEY, "mainnet", "nbis-crwv", ACCOUNT, WALLET, 1)
    assert PRIVATE_KEY not in encrypted and encrypted.startswith("ENC:v1:test-dek-v1:")
    assert crypto.decrypt_storage(encrypted, "mainnet", "nbis-crwv", ACCOUNT, WALLET, 1) == PRIVATE_KEY
    again = crypto.encrypt_storage(PRIVATE_KEY, "mainnet", "nbis-crwv", ACCOUNT, WALLET, 1)
    assert again != encrypted


def test_transport_tamper_and_stale_timestamp_fail_closed(keys) -> None:
    payload, expected = transport_payload(keys)
    tampered = dict(payload)
    tampered["ciphertext"] = b64url(b"x" + base64.urlsafe_b64decode(payload["ciphertext"] + "=" * (-len(payload["ciphertext"]) % 4))[1:])
    with pytest.raises(crypto.WalletCryptoError, match="加密校验失败"):
        crypto.decrypt_transport(tampered, expected)
    stale, stale_expected = transport_payload(keys, 1)
    with pytest.raises(crypto.WalletCryptoError, match="已过期"):
        crypto.decrypt_transport(stale, stale_expected)


def test_storage_wrong_identity_or_tamper_fails_closed(keys) -> None:
    encrypted = crypto.encrypt_storage(PRIVATE_KEY, "mainnet", "nbis-crwv", ACCOUNT, WALLET, 1)
    with pytest.raises(crypto.WalletCryptoError, match="密文校验失败"):
        crypto.decrypt_storage(encrypted, "testnet", "nbis-crwv", ACCOUNT, WALLET, 1)
    with pytest.raises(crypto.WalletCryptoError, match="密文校验失败"):
        crypto.decrypt_storage(encrypted, "mainnet", "nbis-crwv", ACCOUNT, WALLET, 2)
    parts = encrypted.split(":")
    parts[-1] = base64.b64encode(b"x" + base64.b64decode(parts[-1])[1:]).decode()
    with pytest.raises(crypto.WalletCryptoError, match="密文校验失败"):
        crypto.decrypt_storage(":".join(parts), "mainnet", "nbis-crwv", ACCOUNT, WALLET, 1)


def test_missing_or_group_readable_keys_are_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HL_APP_DATA_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.delenv("HL_APP_DATA_ENCRYPTION_KEY_ID", raising=False)
    with pytest.raises(crypto.WalletCryptoError, match="未配置"):
        crypto.encrypt_storage(PRIVATE_KEY, "mainnet", "nbis-crwv", ACCOUNT, WALLET, 1)
    path = tmp_path / "weak.key"
    path.write_bytes(base64.b64encode(os.urandom(32)))
    path.chmod(0o644)
    monkeypatch.setenv("HL_APP_DATA_ENCRYPTION_KEY_FILE", str(path))
    monkeypatch.setenv("HL_APP_DATA_ENCRYPTION_KEY_ID", "test")
    with pytest.raises(crypto.WalletCryptoError, match="权限不安全"):
        crypto.encrypt_storage(PRIVATE_KEY, "mainnet", "nbis-crwv", ACCOUNT, WALLET, 1)
