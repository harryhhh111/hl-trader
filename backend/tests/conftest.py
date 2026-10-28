from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

import psycopg2
import pytest
from psycopg2 import sql


@dataclass
class PostgresNamespace:
    admin_url: str
    migration_url: str
    runtime_url: str
    schema: str
    role: str

    def grant_runtime(self) -> None:
        with psycopg2.connect(self.admin_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(self.schema), sql.Identifier(self.role)
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}").format(
                        sql.Identifier(self.schema), sql.Identifier(self.role)
                    )
                )
                cursor.execute(
                    sql.SQL("REVOKE INSERT, UPDATE, DELETE ON {}.operation_control FROM {}").format(
                        sql.Identifier(self.schema), sql.Identifier(self.role)
                    )
                )


def _schema_url(url: str, schema: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    query["options"] = [f"-csearch_path={schema}"]
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


def _role_url(url: str, role: str, password: str, schema: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{quote(role, safe='')}:{quote(password, safe='')}@{host}"
    return _schema_url(urlunsplit(parts._replace(netloc=netloc)), schema)


@pytest.fixture
def postgres_namespace(request: pytest.FixtureRequest):
    url = os.environ.get("HL_APP_TEST_DATABASE_URL")
    if not url:
        pytest.skip("HL_APP_TEST_DATABASE_URL is required for DB integration")
    schema = f"test_{secrets.token_hex(8)}"
    role = f"runtime_{secrets.token_hex(6)}"
    password = secrets.token_urlsafe(24)
    admin = psycopg2.connect(url)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s").format(sql.Identifier(role)),
                (password,),
            )
        namespace = PostgresNamespace(
            admin_url=url,
            migration_url=_schema_url(url, schema),
            runtime_url=_role_url(url, role, password, schema),
            schema=schema,
            role=role,
        )
        yield namespace
    finally:
        admin.close()
        admin = psycopg2.connect(url)
        admin.autocommit = True
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )
                cursor.execute(
                    sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role))
                )
                cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        finally:
            admin.close()
