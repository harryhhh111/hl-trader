from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from ..db import connect
from ..collectors.quotes import collect_security, enabled_securities
from ..collectors.sources import collect_source, ensure_sec_sources, registered_sources
from ..collectors.sec_client import SECClient
from ..services import ai

WORKER_LIFECYCLE_LOCK_KEY = 0x484C5F574F524B31


class WorkerAlreadyRunning(RuntimeError):
    pass


class WorkerLifecycle:
    def __init__(self, url: str) -> None:
        self.url = url
        self.connection = None

    def acquire(self) -> None:
        conn = connect(self.url, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LIFECYCLE_LOCK_KEY,))
            if not cursor.fetchone()[0]:
                conn.close()
                raise WorkerAlreadyRunning("另一个采集 worker 正在运行")
        self.connection = conn

    def ensure_healthy(self) -> None:
        if self.connection is None or self.connection.closed:
            ai.terminate_models()
            raise RuntimeError("worker 生命周期锁连接失效")
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception as exc:
            self.close()
            ai.terminate_models()
            raise RuntimeError("worker 生命周期锁连接失效") from exc

    def close(self) -> None:
        conn, self.connection = self.connection, None
        if conn is not None:
            conn.close()


def enhance_record(record, source_kind: str, stock_code: str) -> dict:
    if source_kind == "media":
        return ai.enhance_media(record.title, record.excerpt or "", stock_code)
    return ai.enhance_official(record.title, record.form_type, stock_code)


def dry_run(url: str) -> dict:
    return {
        "collectors_enabled": os.environ.get("HL_APP_COLLECTORS_ENABLED", "false") == "true",
        "scheduler_enabled": os.environ.get("HL_APP_SCHEDULER_ENABLED", "false") == "true",
        "tasks": ["quotes", "announcements", "news_enhancement"],
        "quote_targets": [row["stock_code"] for row in enabled_securities(url)],
        "sources": [
            {"source_id": row["source_id"], "kind": row["source_kind"], "issuer": row["issuer_identifier"]}
            for row in registered_sources(url)
        ],
    }


def run_once(url: str, kind: str, http_get=None) -> dict:
    if os.environ.get("HL_APP_COLLECTORS_ENABLED", "false").lower() != "true":
        return {"status": "disabled", "kind": kind}
    from ..migration.service import assert_active

    assert_active(url)
    lifecycle = WorkerLifecycle(url)
    lifecycle.acquire()
    try:
        if kind == "quotes":
            results = []
            for security in enabled_securities(url):
                lifecycle.ensure_healthy()
                results.append({security["stock_code"]: collect_security(url, security)})
            return {"kind": kind, "results": results}
        if kind != "announcements":
            raise ValueError("kind 只支持 quotes 或 announcements")
        ensure_sec_sources(url)
        sources = registered_sources(url)
        client = None
        if any(source["source_kind"] == "sec" for source in sources):
            client = SECClient(url, os.environ.get("HL_APP_SEC_USER_AGENT", ""))
        results = []
        kwargs = {} if http_get is None else {"http_get": http_get}
        for source in sources:
            lifecycle.ensure_healthy()
            results.append(collect_source(url, source, sec_client=client, enhancer=enhance_record, **kwargs))
        return {"kind": kind, "results": results}
    finally:
        lifecycle.close()


def run_scheduler(url: str) -> None:
    if os.environ.get("HL_APP_SCHEDULER_ENABLED", "false").lower() != "true":
        raise RuntimeError("HL_APP_SCHEDULER_ENABLED 未启用")
    interval = int(os.environ.get("HL_APP_COLLECTION_INTERVAL_SECONDS", "3600"))
    while True:
        run_once(url, "quotes")
        run_once(url, "announcements")
        time.sleep(max(60, interval))
