from __future__ import annotations

import math
import os
import threading
import time
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import psycopg2

from ..db import connect

GATE_KEY = "sec_edgar"
LOCK_KEY = 0x53454347415445


class SECError(RuntimeError):
    pass


class SECCoordinationError(SECError):
    pass


class SECWaitTimeout(SECError):
    pass


class SECTransportError(SECError):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Gate:
    def __init__(self, conn, interval: float) -> None:
        self.conn = conn
        self.interval = interval

    def reserve(self, duration: float) -> None:
        with self.conn.cursor() as cursor:
            cursor.execute(
                "UPDATE sec_request_gate SET next_allowed_at = clock_timestamp() + %s * interval '1 second', updated_at = clock_timestamp() WHERE gate_key = %s",
                (duration + self.interval + 2, GATE_KEY),
            )

    def finish(self, cooldown: float) -> None:
        with self.conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE sec_request_gate SET next_allowed_at = clock_timestamp() + %s * interval '1 second',
                    blocked_until = GREATEST(blocked_until, clock_timestamp() + %s * interval '1 second'),
                    updated_at = clock_timestamp() WHERE gate_key = %s
                """,
                (self.interval, cooldown, GATE_KEY),
            )


class SECClient:
    def __init__(self, database_url: str, user_agent: str, rate: int = 2, allow_insecure_for_tests: bool = False) -> None:
        if rate not in {1, 2}:
            raise SECError("SEC rate must be 1 or 2")
        if not user_agent.strip() or "\n" in user_agent or "\r" in user_agent:
            raise SECError("HL_APP_SEC_USER_AGENT must be a nonempty single line")
        self.database_url = database_url
        self.user_agent = user_agent
        self.interval = 1.0 / rate
        self.allow_insecure_for_tests = allow_insecure_for_tests
        self._process_lock = threading.Lock()

    def _validate_url(self, url: str) -> None:
        parsed = urlsplit(url)
        secure = parsed.scheme == "https" and parsed.hostname in {"www.sec.gov", "data.sec.gov"} and parsed.port in {None, 443}
        local_test = self.allow_insecure_for_tests and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if not (secure or local_test) or parsed.username or parsed.password or parsed.fragment:
            raise SECError("SEC URL is outside the allowed endpoint boundary")

    def _connect(self):
        return connect(self.database_url, connect_timeout=2, application_name="sec_request_gate")

    @contextmanager
    def _acquire(self, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not self._process_lock.acquire(timeout=remaining):
            raise SECWaitTimeout("SEC in-process queue deadline exceeded")
        conn = None
        try:
            conn = self._connect()
            conn.autocommit = True
            while True:
                if time.monotonic() >= deadline:
                    raise SECWaitTimeout("SEC lock wait deadline exceeded")
                with conn.cursor() as cursor:
                    cursor.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
                    locked = cursor.fetchone()[0]
                    if locked:
                        cursor.execute(
                            "SELECT EXTRACT(EPOCH FROM GREATEST(next_allowed_at, blocked_until) - clock_timestamp()) FROM sec_request_gate WHERE gate_key = %s",
                            (GATE_KEY,),
                        )
                        row = cursor.fetchone()
                        if row is None or not math.isfinite(float(row[0])):
                            raise SECCoordinationError("SEC gate row missing or invalid")
                        wait = max(0.0, float(row[0]))
                        if wait == 0:
                            break
                        cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
                    else:
                        wait = 0.05
                if wait >= deadline - time.monotonic():
                    raise SECWaitTimeout("SEC shared gate wait deadline exceeded")
                time.sleep(wait)
            yield _Gate(conn, self.interval)
        except psycopg2.Error as exc:
            raise SECCoordinationError("SEC coordination unavailable") from exc
        finally:
            if conn is not None:
                conn.close()
            self._process_lock.release()

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            if value.strip().isdigit():
                return max(0.0, float(value.strip()))
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                from datetime import timezone
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _cooldown(status: int | None, retries: int, retry_after: float | None) -> float:
        if status == 403:
            return 60.0
        cooldown = float(2 ** (retries + 1))
        if status in {429, 503}:
            cooldown = max(cooldown, retry_after if retry_after is not None else (10.0 if status == 429 else 0.0))
        return cooldown

    def get(self, url: str, timeout: float = 15, total_timeout: float = 60) -> bytes:
        self._validate_url(url)
        if not 0 < timeout <= 60 or not 0 < total_timeout <= 180:
            raise SECError("invalid SEC time budget")
        deadline = time.monotonic() + total_timeout
        retries = 0
        opener = build_opener(_NoRedirect)
        while True:
            status = None
            body = None
            error = None
            retry_after = None
            with self._acquire(deadline) as gate:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SECWaitTimeout("SEC deadline exceeded before send")
                gate.reserve(min(timeout, remaining))
                request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
                try:
                    with opener.open(request, timeout=min(timeout, remaining)) as response:
                        status = response.status
                        body = response.read(16 * 1024 * 1024 + 1)
                        if len(body) > 16 * 1024 * 1024:
                            raise SECTransportError("SEC response too large")
                except HTTPError as exc:
                    status = exc.code
                    retry_after = self._retry_after(exc.headers.get("Retry-After")) if status in {429, 503} else None
                    error = SECTransportError(f"SEC HTTP {status}", status, retry_after)
                except (URLError, TimeoutError, OSError) as exc:
                    error = SECTransportError("SEC transport failure")
                cooldown = self._cooldown(status, retries, retry_after) if error is not None else 0.0
                gate.finish(cooldown)
            if error is None:
                return body
            if status in {301, 302, 303, 307, 308}:
                raise SECError("SEC redirects require explicit endpoint validation")
            if retries >= 2:
                raise error
            retries += 1
