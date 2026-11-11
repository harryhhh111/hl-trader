from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from hl_app.services import hyperliquid_pairs as pairs
from hl_app.services import hyperliquid_quotes as quotes
from hl_app.services.hyperliquid_adjustments import AdjustmentError

from .test_hyperliquid_pairs import VALID


def payload() -> dict:
    revision = pairs.load_pairs()[0]
    return {
        "schema_version": 1,
        "pair_id": "nbis-crwv",
        "network": "mainnet",
        "config_revision": revision,
        "queried_at": "2026-11-10T03:34:27Z",
        "legs": [
            {"coin": "xyz:NBIS", "bid": "9.9", "ask": "10.1", "mid": "10", "book_time": "2026-11-10T03:34:26.900Z"},
            {"coin": "xyz:CRWV", "bid": "49.75", "ask": "50.25", "mid": "50", "book_time": "2026-11-10T03:34:26.900Z"},
        ],
    }


@pytest.fixture(autouse=True)
def configured(monkeypatch, tmp_path):
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps(VALID), encoding="utf-8")
    monkeypatch.setenv("HL_APP_PAIRS_CONFIG", str(path))
    monkeypatch.setenv("HL_APP_TRADER_BIN", "/bin/hl")
    pairs.clear_cache()
    quotes.clear_quote_cache()
    yield
    pairs.clear_cache()
    quotes.clear_quote_cache()


def mock_success(monkeypatch, response=None, delay: float = 0):
    calls = []
    response = response or payload()
    monkeypatch.setattr(
        quotes.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=0),
    )

    def communicate(_proc, _timeout):
        if delay:
            time.sleep(delay)
        return json.dumps(response), ""

    monkeypatch.setattr(quotes, "communicate_bounded", communicate)
    return calls


def test_quote_identity_and_success_cache(monkeypatch) -> None:
    calls = mock_success(monkeypatch)
    first = quotes.get_adjustment_quote("nbis-crwv", pairs.load_pairs()[0])
    second = quotes.get_adjustment_quote("nbis-crwv", pairs.load_pairs()[0])
    assert first == second
    assert first["legs"][0]["mid"] == "10"
    assert len(calls) == 1
    assert "adjust-quote" in calls[0]
    assert "--execute" not in calls[0]


def test_revision_mismatch_is_rejected_before_process(monkeypatch) -> None:
    calls = mock_success(monkeypatch)
    with pytest.raises(AdjustmentError) as error:
        quotes.get_adjustment_quote("nbis-crwv", "0" * 64)
    assert error.value.status_code == 409
    assert calls == []


def test_invalid_mid_is_rejected(monkeypatch) -> None:
    invalid = payload()
    invalid["legs"][0]["mid"] = "11"
    mock_success(monkeypatch, invalid)
    with pytest.raises(AdjustmentError, match="中间价"):
        quotes.get_adjustment_quote("nbis-crwv", pairs.load_pairs()[0])


def test_process_failure_is_not_cached(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        quotes.subprocess,
        "Popen",
        lambda argv, **kwargs: calls.append(argv) or SimpleNamespace(returncode=1),
    )
    monkeypatch.setattr(quotes, "communicate_bounded", lambda *_args: ("", "failed"))
    for _ in range(2):
        with pytest.raises(AdjustmentError, match="盘口读取失败"):
            quotes.get_adjustment_quote("nbis-crwv", pairs.load_pairs()[0])
    assert len(calls) == 2


def test_same_key_concurrent_requests_share_collection(monkeypatch) -> None:
    calls = mock_success(monkeypatch, delay=0.1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: quotes.get_adjustment_quote("nbis-crwv", pairs.load_pairs()[0]),
            range(2),
        ))
    assert len(calls) == 1
    assert results[0] == results[1]
