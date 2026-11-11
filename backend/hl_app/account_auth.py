from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from psycopg2 import errors

from .auth import AUTH_LOCK_KEY, AuthError, SessionCredential, SessionInfo, _hash
from .db import connect

PASSWORD_ITERATIONS = 600_000
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DUMMY_PASSWORD_HASH = "pbkdf2_sha256$600000$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise AuthError(422, "请输入有效邮箱地址")
    return email


def _password_hash(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise AuthError(422, "密码长度必须为 12 到 128 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "pbkdf2_sha256$600000$" + base64.urlsafe_b64encode(salt).decode().rstrip("=") + "$" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), base64.urlsafe_b64decode(salt + "=" * (-len(salt) % 4)), int(iterations),
        )
        return hmac.compare_digest(base64.urlsafe_b64encode(digest).decode().rstrip("="), expected)
    except (ValueError, TypeError):
        return False


class AccountAuthService:
    def __init__(self, url: str, ttl_seconds: int = 604800) -> None:
        self._url = url
        self._ttl_seconds = ttl_seconds

    def verify_configuration(self) -> None:
        with connect(self._url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM auth_accounts LIMIT 1")

    def _issue_session(self, cursor, account_id: int, email: str) -> SessionCredential:
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        cursor.execute("SELECT now()")
        expires_at = cursor.fetchone()[0] + timedelta(seconds=self._ttl_seconds)
        cursor.execute(
            """
            INSERT INTO auth_sessions (session_sha256, csrf_sha256, auth_epoch, account_id, expires_at)
            VALUES (%s, %s, 1, %s, %s)
            """,
            (_hash(session_token), _hash(csrf_token), account_id, expires_at),
        )
        return SessionCredential(session_token, csrf_token, expires_at)

    def register(self, email: str, password: str) -> SessionCredential:
        email = normalize_email(email)
        password_hash = _password_hash(password)
        try:
            with connect(self._url) as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (AUTH_LOCK_KEY,))
                    cursor.execute("SELECT count(*) FROM auth_accounts")
                    first = cursor.fetchone()[0] == 0
                    cursor.execute(
                        "INSERT INTO auth_accounts (email, password_hash) VALUES (%s, %s) RETURNING id",
                        (email, password_hash),
                    )
                    account_id = cursor.fetchone()[0]
                    if first:
                        for table in (
                            "hyperliquid_pair_accounts",
                            "hyperliquid_api_wallets",
                            "hyperliquid_execution_requests",
                        ):
                            cursor.execute(
                                f"UPDATE {table} SET owner_account_id = %s WHERE owner_account_id IS NULL",
                                (account_id,),
                            )
                    return self._issue_session(cursor, account_id, email)
        except errors.UniqueViolation as exc:
            raise AuthError(409, "该邮箱已注册，请直接登录") from exc

    def login(self, email: str, password: str) -> SessionCredential:
        email = normalize_email(email)
        if not 1 <= len(password) <= 128:
            raise AuthError(401, "邮箱或密码错误")
        failure = None
        credential = None
        with connect(self._url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (AUTH_LOCK_KEY,))
                cursor.execute(
                    "SELECT window_started_at, failures, locked_until FROM login_rate_limit WHERE singleton = true FOR UPDATE"
                )
                _window, failures, locked_until = cursor.fetchone()
                cursor.execute("SELECT now()")
                now = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT id, email, password_hash FROM auth_accounts WHERE email = %s AND enabled = true",
                    (email,),
                )
                account = cursor.fetchone()
                valid = _password_matches(password, account[2] if account else DUMMY_PASSWORD_HASH)
                if locked_until is not None and locked_until > now:
                    failure = AuthError(429, "登录尝试过多，请稍后重试")
                elif account is None or not valid:
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
                    failure = AuthError(401, "邮箱或密码错误")
                else:
                    cursor.execute(
                        "UPDATE login_rate_limit SET failures = 0, locked_until = NULL, window_started_at = now() WHERE singleton = true"
                    )
                    credential = self._issue_session(cursor, account[0], account[1])
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
                cursor.execute(
                    """
                    SELECT s.csrf_sha256, s.expires_at, a.id, a.email
                    FROM auth_sessions s JOIN auth_accounts a ON a.id = s.account_id
                    WHERE s.session_sha256 = %s AND s.revoked_at IS NULL
                      AND s.expires_at > now() AND a.enabled = true
                    """,
                    (session_hash,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise AuthError(401, "会话无效或已过期")
        return SessionInfo(session_hash, row[0], 1, row[1], row[3], row[2])

    def logout(self, session: SessionInfo) -> None:
        with connect(self._url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE auth_sessions SET revoked_at = now() WHERE session_sha256 = %s AND revoked_at IS NULL",
                    (session.session_sha256,),
                )
