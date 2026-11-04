from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

DIRECTORY = Path("/var/lib/hl-wallet-keys")


def create(path: Path, content: bytes) -> str:
    if path.exists():
        return "preserved"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return "created"


def main() -> None:
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    data = create(DIRECTORY / "data-encryption.key", base64.b64encode(os.urandom(32)))
    transport = create(
        DIRECTORY / "transport-rsa-private.pem",
        rsa.generate_private_key(public_exponent=65537, key_size=3072).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    print(f"data-encryption={data} transport-rsa={transport}")


if __name__ == "__main__":
    main()
