from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from hl_app.services import hyperliquid_pairs as pairs

from .test_hyperliquid_pairs import VALID, v2_snapshot


def configure(monkeypatch, tmp_path):
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(path))
    pairs.clear_cache()
    pairs.clear_runtime_cache()
    revision, descriptors = pairs.load_pairs()
    return revision, descriptors[0]


def test_same_identity_uses_single_flight(monkeypatch, tmp_path) -> None:
    revision, descriptor = configure(monkeypatch, tmp_path)
    calls = []
    guard = threading.Lock()

    def run(_argv):
        with guard:
            calls.append(1)
        time.sleep(0.1)
        return v2_snapshot(descriptor, revision)

    monkeypatch.setattr(pairs, "_run_dashboard", run)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _i: pairs.get_pair_dashboard(descriptor["id"]), range(8)))
    assert len(calls) == 1
    pairs.clear_runtime_cache()


def test_collector_semaphore_limits_all_snapshot_processes(monkeypatch, tmp_path) -> None:
    revision, descriptor = configure(monkeypatch, tmp_path)
    guard = threading.Lock()
    active = 0
    maximum = 0
    calls = 0

    monkeypatch.setattr(
        pairs, "_cache_key", lambda kind, _revision, _descriptor: f"{kind}:{threading.get_ident()}"
    )

    def run(_argv):
        nonlocal active, maximum, calls
        with guard:
            active += 1
            calls += 1
            maximum = max(maximum, active)
        time.sleep(0.1)
        with guard:
            active -= 1
        return v2_snapshot(descriptor, revision)

    monkeypatch.setattr(pairs, "_run_dashboard", run)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _i: pairs.get_pair_dashboard(descriptor["id"]), range(8)))
    assert calls == 8
    assert maximum == pairs.MAX_COLLECTOR_PROCESSES == 2
    pairs.clear_runtime_cache()
