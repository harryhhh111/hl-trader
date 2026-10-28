from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

import os
from fastapi import HTTPException, Request

from .config import ConfigError, database_url, optional_value
from .db import connect

AUTH_LOCK_KEY = 0x484C5F4155544831
SESSION_COOKIE = "hl_session"
CSRF_COOKIE = "hl_csrf"
CSRF_HEADER = "x-csrf-token"


class AuthError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class SessionCredential:
    session_token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class SessionInfo:
    session_sha256: str
    csrf_sha256: str
    auth_epoch: int
    expires_at: datetime


def _token_bytes(token: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception as exc:
        raise ConfigError("HL_APP_AUTH_TOKEN must be base64url random data") from exc
    canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if len(raw) < 32 or not hmac.compare_digest(canonical, token.rstrip("=")):
        raise ConfigError("HL_APP_AUTH_TOKEN must contain at least 256 bits of random data")
    return raw


def token_fingerprint(token: str) -> str:
    _token_bytes(token)
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _auth_token(environ: Mapping[str, str] | None = None) -> str:
    token = optional_value("HL_APP_AUTH_TOKEN", environ)
    if token is None:
        raise ConfigError("HL_APP_AUTH_TOKEN is required")
    token_fingerprint(token)
    return token


def initialize(url: str, token: str) -> int:
    fingerprint = token_fingerprint(token)
    with connect(url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (AUTH_LOCK_KEY,))
            cursor.execute("SELECT auth_epoch FROM auth_control WHERE singleton = true FOR UPDATE")
            if cursor.fetchone():
                raise AuthError(409, "认证已初始化，拒绝覆盖")
            cursor.execute(
                "INSERT INTO auth_control (auth_epoch, token_sha256) VALUES (1, %s)",
                (fingerprint,),
            )
    return 1


def rotate(url: str, token: str) -> int:
    fingerprint = token_fingerprint(token)
    with connect(url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (AUTH_LOCK_KEY,))
            cursor.execute(
                "SELECT auth_epoch, token_sha256 FROM auth_control WHERE singleton = true FOR UPDATE"
            )
            row = cursor.fetchone()
            if row is None:
                raise AuthError(409, "认证尚未初始化")
            epoch, current_fingerprint = row
            if hmac.compare_digest(current_fingerprint, fingerprint):
                return epoch
            epoch += 1
            cursor.execute(
                "UPDATE auth_control SET auth_epoch = %s, token_sha256 = %s, updated_at = now() WHERE singleton = true",
                (epoch, fingerprint),
            )
            cursor.execute("UPDATE auth_sessions SET revoked_at = now() WHERE revoked_at IS NULL")
    return epoch


class AuthService:
    def __init__(self, url: str, token: str, ttl_seconds: int = 604800) -> None:
        self._url = url
        self._fingerprint = token_fingerprint(token)
        self._ttl_seconds = ttl_seconds

    def _control(self, cursor) -> int:
        cursor.execute("SELECT auth_epoch, token_sha256 FROM auth_control WHERE singleton = true")
        row = cursor.fetchone()
        if row is None:
            raise AuthError(503, "认证尚未初始化")
        epoch, fingerprint = row
        if not hmac.compare_digest(fingerprint, self._fingerprint):
            raise AuthError(503, "认证配置与控制记录不一致，请完成轮换并使用新配置")
        return epoch

    def verify_configuration(self) -> None:
        with connect(self._url) as conn:
            with conn.cursor() as cursor:
                self._control(cursor)

    def login(self, token: str) -> SessionCredential:
        try:
            candidate = token_fingerprint(token)
        except ConfigError as exc:
            raise AuthError(401, "登录 token 无效") from exc
        failure: AuthError | None = None
        credential: SessionCredential | None = None
        with connect(self._url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (AUTH_LOCK_KEY,))
                epoch = self._control(cursor)
                cursor.execute(
                    "SELECT window_started_at, failures, locked_until FROM login_rate_limit WHERE singleton = true FOR UPDATE"
                )
                _window, failures, locked_until = cursor.fetchone()
                cursor.execute("SELECT now()")
                now = cursor.fetchone()[0]
                if locked_until is not None and locked_until > now:
                    failure = AuthError(429, "登录尝试过多，请稍后重试")
                elif not hmac.compare_digest(candidate, self._fingerprint):
                    cursor.execute(
                        """
                        UPDATE login_rate_limit SET
                            window_started_at = CASE WHEN window_started_at < now() - interval '5 minutes' THEN now() ELSE window_started_at END,
                            failures = CASE WHEN window_started_at < now() - interval '5 minutes' THEN 1 ELSE failures + 1 END,
                            locked_until = CASE
                                WHEN window_started_at >= now() - interval '5 minutes' AND failures + 1 >= 5 THEN now() + interval '15 minutes'
                                WHEN window_started_at < now() - interval '5 minutes' THEN NULL
                                ELSE locked_until
                            END
                        WHERE singleton = true
                        """
                    )
                    failure = AuthError(401, "登录 token 无效")
                else:
                    session_token = secrets.token_urlsafe(32)
                    csrf_token = secrets.token_urlsafe(32)
                    expires_at = now + timedelta(seconds=self._ttl_seconds)
                    cursor.execute(
                        "INSERT INTO auth_sessions (session_sha256, csrf_sha256, auth_epoch, expires_at) VALUES (%s, %s, %s, %s)",
                        (_hash(session_token), _hash(csrf_token), epoch, expires_at),
                    )
                    cursor.execute(
                        "UPDATE login_rate_limit SET failures = 0, locked_until = NULL, window_started_at = now() WHERE singleton = true"
                    )
                    credential = SessionCredential(session_token, csrf_token, expires_at)
        if failure is not None:
            raise failure
        assert credential is not None
        return credential

    def authenticate(self, session_token: str | None) -> SessionInfo:
        if not session_token:
            raise AuthError(401, "未登录")
        session_hash = _hash(session_token)
        with connect(self._url) as conn:
            with conn.cursor() as cursor:
                epoch = self._control(cursor)
                cursor.execute(
                    """
                    SELECT csrf_sha256, auth_epoch, expires_at FROM auth_sessions
                    WHERE session_sha256 = %s AND revoked_at IS NULL AND expires_at > now()
                    """,
                    (session_hash,),
                )
                row = cursor.fetchone()
                if row is None or row[1] != epoch:
                    raise AuthError(401, "会话无效或已过期")
        return SessionInfo(session_hash, row[0], row[1], row[2])

    def logout(self, session: SessionInfo) -> None:
        with connect(self._url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE auth_sessions SET revoked_at = now() WHERE session_sha256 = %s AND revoked_at IS NULL",
                    (session.session_sha256,),
                )


def check_csrf(request: Request, session: SessionInfo) -> None:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get(CSRF_HEADER, "")
    if not cookie or not hmac.compare_digest(cookie, header) or not hmac.compare_digest(_hash(cookie), session.csrf_sha256):
        raise HTTPException(403, "CSRF 校验失败")


def require_session(request: Request) -> SessionInfo:
    lifecycle = request.app.state.web_lifecycle
    try:
        lifecycle.ensure_healthy()
    except Exception as exc:
        from .services import hyperliquid_pairs

        hyperliquid_pairs.terminate_collectors()
        request.app.state.initiate_shutdown()
        raise HTTPException(503, "Web 生命周期锁已失效") from exc
    try:
        session = request.app.state.auth.authenticate(request.cookies.get(SESSION_COOKIE))
    except AuthError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_csrf(request, session)
    request.state.session = session
    return session


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hl_app.auth")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("initialize")
    subparsers.add_parser("rotate")
    args = parser.parse_args(argv)
    url = database_url("migration")
    token = _auth_token(os.environ)
    if args.command == "initialize":
        epoch = initialize(url, token)
    else:
        epoch = rotate(url, token)
    print(f"auth_epoch={epoch}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigError, AuthError) as exc:
        message = exc.detail if isinstance(exc, AuthError) else str(exc)
        raise SystemExit(message) from exc
