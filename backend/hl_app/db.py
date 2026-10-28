from __future__ import annotations

import argparse
from contextlib import closing
from importlib import resources
from typing import Sequence

import psycopg2
from psycopg2.extensions import connection

from .config import ConfigError, database_url

MIGRATION_LOCK_KEY = 0x484C5F4D49475231
WEB_LIFECYCLE_LOCK_KEY = 0x484C5F5745423031


class WebInstanceAlreadyRunning(RuntimeError):
    pass


class WebLifecycleLost(RuntimeError):
    pass


def connect(url: str) -> connection:
    return psycopg2.connect(url, connect_timeout=10)


def _migration_files() -> list[tuple[str, str]]:
    migrations = resources.files("hl_app.migrations")
    return [
        (entry.name.removesuffix(".sql"), entry.read_text(encoding="utf-8"))
        for entry in sorted(migrations.iterdir(), key=lambda item: item.name)
        if entry.name.endswith(".sql")
    ]


def migrate(url: str) -> list[str]:
    applied: list[str] = []
    with closing(connect(url)) as conn, conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            for version, sql in _migration_files():
                cursor.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = %s", (version,)
                )
                if cursor.fetchone():
                    continue
                cursor.execute(sql)
                cursor.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
                )
                applied.append(version)
    return applied


class WebLifecycle:
    def __init__(self, url: str) -> None:
        self._url = url
        self._connection: connection | None = None

    def acquire(self) -> None:
        if self._connection is not None:
            raise RuntimeError("Web lifecycle lock already acquired")
        conn = connect(self._url)
        conn.autocommit = True
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", (WEB_LIFECYCLE_LOCK_KEY,))
                acquired = cursor.fetchone()[0]
            if not acquired:
                raise WebInstanceAlreadyRunning("another Web instance holds the lifecycle lock")
            self._connection = conn
        except Exception:
            conn.close()
            raise

    def ensure_healthy(self) -> None:
        conn = self._connection
        if conn is None or conn.closed:
            raise WebLifecycleLost("Web lifecycle lock connection is unavailable")
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception as exc:
            self.close()
            raise WebLifecycleLost("Web lifecycle lock connection was lost") from exc

    def close(self) -> None:
        conn, self._connection = self._connection, None
        if conn is not None:
            conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hl_app.db")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate")
    args = parser.parse_args(argv)
    if args.command == "migrate":
        for version in migrate(database_url("migration")):
            print(version)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
