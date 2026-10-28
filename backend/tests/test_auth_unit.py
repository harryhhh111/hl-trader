from __future__ import annotations

import base64

import pytest

from hl_app.auth import token_fingerprint
from hl_app.config import ConfigError


def test_base64url_random_token_has_stable_fingerprint() -> None:
    token = base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("=")
    assert token_fingerprint(token) == token_fingerprint(token)
    assert len(token_fingerprint(token)) == 64


@pytest.mark.parametrize("token", ["short", "A" * 43 + "!", "ordinary-password"])
def test_weak_or_malformed_token_is_rejected(token: str) -> None:
    with pytest.raises(ConfigError):
        token_fingerprint(token)
