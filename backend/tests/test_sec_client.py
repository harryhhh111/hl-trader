from __future__ import annotations

import multiprocessing
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg2
import pytest

from hl_app.collectors.sec_client import SECClient, SECCoordinationError, SECError
from hl_app.db import migrate

from .conftest import PostgresNamespace

pytestmark = pytest.mark.db
UA = "hl-app-tests contact@example.invalid"


class Handler(BaseHTTPRequestHandler):
    status = 200
    retry_after = None

    def do_GET(self):
        self.server.request_times.append(time.monotonic())
        self.server.count += 1
        self.send_response(self.status)
        if self.retry_after is not None:
            self.send_header("Retry-After", self.retry_after)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args):
        pass


def worker(url, database_url, count, queue):
    client = SECClient(database_url, UA, allow_insecure_for_tests=True)
    for _ in range(count):
        client.get(url, total_timeout=20)
    queue.put(True)


@pytest.fixture
def gate_db(postgres_namespace: PostgresNamespace):
    migrate(postgres_namespace.migration_url)
    postgres_namespace.grant_runtime()
    return postgres_namespace


def local_server(status=200, retry_after=None):
    handler = type("FixtureHandler", (Handler,), {"status": status, "retry_after": retry_after})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.request_times = []
    server.count = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_sec_gate_limits_multiple_processes(gate_db) -> None:
    server, thread = local_server()
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    url = f"http://127.0.0.1:{server.server_port}/fixture"
    processes = [
        context.Process(target=worker, args=(url, gate_db.runtime_url, 2, queue))
        for _ in range(4)
    ]
    started = time.monotonic()
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
        assert all(process.exitcode == 0 for process in processes)
        assert queue.qsize() == 4
        assert server.count == 8
        elapsed = time.monotonic() - started
        assert elapsed >= 3.0
        intervals = [b - a for a, b in zip(sorted(server.request_times), sorted(server.request_times)[1:])]
        assert all(interval >= 0.42 for interval in intervals)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join()
        server.shutdown()
        server.server_close()
        thread.join()
        queue.close()


def test_sec_cooldown_is_shared_and_retried(gate_db) -> None:
    server, thread = local_server(status=429, retry_after="1")
    url = f"http://127.0.0.1:{server.server_port}/fixture"
    client = SECClient(gate_db.runtime_url, UA, allow_insecure_for_tests=True)
    try:
        with pytest.raises(Exception):
            client.get(url, total_timeout=15)
        assert server.count == 3
        assert server.request_times[1] - server.request_times[0] >= 0.8
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_coordination_failure_never_falls_back_to_ungated_request(gate_db) -> None:
    server, thread = local_server()
    with psycopg2.connect(gate_db.migration_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM sec_request_gate")
    client = SECClient(gate_db.runtime_url, UA, allow_insecure_for_tests=True)
    try:
        with pytest.raises(SECCoordinationError):
            client.get(f"http://127.0.0.1:{server.server_port}/fixture", total_timeout=5)
        assert server.count == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_sec_rejects_arbitrary_url(gate_db) -> None:
    client = SECClient(gate_db.runtime_url, UA)
    with pytest.raises(SECError):
        client.get("https://example.com/data")
