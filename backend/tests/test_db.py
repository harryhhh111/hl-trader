from __future__ import annotations

import psycopg2
import pytest
from psycopg2 import errors

from hl_app.db import WebInstanceAlreadyRunning, WebLifecycle, migrate

from .conftest import PostgresNamespace

pytestmark = pytest.mark.db


def test_migration_is_idempotent_and_defaults_to_maintenance(
    postgres_namespace: PostgresNamespace,
) -> None:
    assert migrate(postgres_namespace.migration_url) == ["001_operation_control", "002_auth"]
    assert migrate(postgres_namespace.migration_url) == []
    with psycopg2.connect(postgres_namespace.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT mode FROM operation_control")
            assert cursor.fetchone() == ("maintenance",)


def test_runtime_role_has_no_ddl_or_control_write_permission(
    postgres_namespace: PostgresNamespace,
) -> None:
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    with psycopg2.connect(postgres_namespace.runtime_url) as conn:
        with conn.cursor() as cursor:
            with pytest.raises(errors.InsufficientPrivilege):
                cursor.execute("CREATE TABLE forbidden_runtime_ddl (id INTEGER)")
        conn.rollback()
        with conn.cursor() as cursor:
            with pytest.raises(errors.InsufficientPrivilege):
                cursor.execute("UPDATE operation_control SET mode = 'active'")


def test_web_lifecycle_lock_rejects_second_process_connection(
    postgres_namespace: PostgresNamespace,
) -> None:
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    first = WebLifecycle(postgres_namespace.runtime_url)
    second = WebLifecycle(postgres_namespace.runtime_url)
    first.acquire()
    try:
        with pytest.raises(WebInstanceAlreadyRunning):
            second.acquire()
        first.ensure_healthy()
    finally:
        first.close()
        second.close()
    third = WebLifecycle(postgres_namespace.runtime_url)
    third.acquire()
    third.close()
