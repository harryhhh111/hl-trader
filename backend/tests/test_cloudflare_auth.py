from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.exceptions import PyJWKClientError

from hl_app import api
from hl_app.auth import AuthError, CF_ACCESS_JWT_HEADER, CloudflareAccessService

TEAM_DOMAIN = "https://team.cloudflareaccess.com"
AUDIENCE = "a" * 64
ORIGIN = "https://hl-trader.nfprompt.io"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = PRIVATE_KEY.public_key()


class LocalJWKClient:
    def get_signing_key_from_jwt(self, _assertion):
        return PUBLIC_KEY


class BrokenJWKClient:
    def get_signing_key_from_jwt(self, _assertion):
        raise PyJWKClientError("offline")


class Lifecycle:
    def acquire(self):
        return None

    def ensure_healthy(self):
        return None

    def close(self):
        return None


def access_token(**changes) -> str:
    now = int(time.time())
    claims = {
        "aud": [AUDIENCE],
        "email": "member@example.com",
        "exp": now + 300,
        "iat": now - 5,
        "nbf": now - 5,
        "iss": TEAM_DOMAIN,
        "type": "app",
        "sub": "user-id",
    }
    claims.update(changes)
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "local"})


def service() -> CloudflareAccessService:
    return CloudflareAccessService(TEAM_DOMAIN, AUDIENCE, LocalJWKClient())


@pytest.fixture
def client(monkeypatch, tmp_path):
    pair_config = tmp_path / "pairs.json"
    pair_config.write_text(json.dumps({"config_version": 1, "pairs": []}), encoding="utf-8")
    monkeypatch.setenv("HL_APP_DATABASE_URL", "postgresql://runtime.invalid/hl")
    monkeypatch.setenv("HL_APP_AUTH_MODE", "cloudflare_access")
    monkeypatch.delenv("HL_APP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("HL_APP_CF_TEAM_DOMAIN", TEAM_DOMAIN)
    monkeypatch.setenv("HL_APP_CF_ACCESS_AUD", AUDIENCE)
    monkeypatch.setenv("HL_APP_SECURE_COOKIE", "true")
    monkeypatch.setenv("HL_APP_TRUSTED_ORIGINS", ORIGIN)
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(pair_config))
    monkeypatch.setattr(api, "WebLifecycle", lambda _url: Lifecycle())
    monkeypatch.setattr(api, "CloudflareAccessService", lambda *_args: service())
    monkeypatch.setattr(api.hyperliquid_managed_adjustments, "mark_interrupted_requests", lambda _url: None)
    with TestClient(api.create_app()) as test_client:
        yield test_client


def test_valid_access_jwt_authenticates_session_and_data(client) -> None:
    headers = {CF_ACCESS_JWT_HEADER: access_token()}
    session = client.get("/api/v1/session", headers=headers)
    assert session.status_code == 200
    assert session.json()["data"] | {"expires_at": None} == {
        "authenticated": True,
        "auth_mode": "cloudflare_access",
        "email": "member@example.com",
        "expires_at": None,
    }
    assert client.get("/api/v1/hyperliquid/pairs", headers=headers).status_code == 200


def test_missing_jwt_and_spoofed_email_are_rejected(client) -> None:
    session = client.get("/api/v1/session")
    assert session.status_code == 200
    assert session.json()["data"] == {
        "authenticated": False,
        "auth_mode": "cloudflare_access",
    }
    assert client.get("/api/v1/hyperliquid/pairs").status_code == 401
    assert client.get(
        "/api/v1/hyperliquid/pairs",
        headers={"Cf-Access-Authenticated-User-Email": "member@example.com"},
    ).status_code == 401


def test_health_is_public_but_token_login_is_disabled(client) -> None:
    assert client.get("/api/v1/health").status_code == 200
    response = client.post(
        "/api/v1/session/login",
        json={"token": "unused"},
        headers={"Origin": ORIGIN, CF_ACCESS_JWT_HEADER: access_token()},
    )
    assert response.status_code == 400


def test_access_write_requests_keep_origin_check_without_token_csrf(client) -> None:
    headers = {CF_ACCESS_JWT_HEADER: access_token()}
    assert client.post("/api/v1/watchlist/quotes", json={}, headers=headers).status_code == 403
    response = client.post(
        "/api/v1/watchlist/quotes", json={}, headers={**headers, "Origin": ORIGIN}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": ["b" * 64]},
        {"iss": "https://other.cloudflareaccess.com"},
        {"exp": int(time.time()) - 60},
        {"nbf": int(time.time()) + 300},
        {"type": "org"},
        {"email": ""},
    ],
)
def test_invalid_claims_are_rejected(claims) -> None:
    with pytest.raises(AuthError) as error:
        service().authenticate(access_token(**claims))
    assert error.value.status_code == 401


def test_bad_signature_and_jwks_failure_fail_closed() -> None:
    assertion = access_token()
    header_and_payload, signature = assertion.rsplit(".", 1)
    tampered = f"{header_and_payload}.{'A' if signature[0] != 'A' else 'B'}{signature[1:]}"
    with pytest.raises(AuthError) as invalid:
        service().authenticate(tampered)
    assert invalid.value.status_code == 401
    unavailable = CloudflareAccessService(TEAM_DOMAIN, AUDIENCE, BrokenJWKClient())
    with pytest.raises(AuthError) as error:
        unavailable.authenticate(assertion)
    assert error.value.status_code == 503
