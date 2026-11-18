from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hl_app import workers


@pytest.mark.parametrize("master,override,expected", [
    ("false", None, False), ("true", None, True),
    ("true", "", True), ("false", "true", True),
    ("true", "false", False), ("false", " TRUE ", True),
])
def test_quotes_switch_can_override_or_inherit_master(monkeypatch, master, override, expected):
    monkeypatch.setenv("HL_APP_COLLECTORS_ENABLED", master)
    monkeypatch.delenv("HL_APP_QUOTES_ENABLED", raising=False)
    if override is not None:
        monkeypatch.setenv("HL_APP_QUOTES_ENABLED", override)
    assert workers._quotes_enabled() is expected


def test_quotes_only_runs_collection_without_enabling_news(monkeypatch):
    monkeypatch.setenv("HL_APP_COLLECTORS_ENABLED", "false")
    monkeypatch.setenv("HL_APP_QUOTES_ENABLED", "true")
    monkeypatch.setenv("HL_APP_NEWS_SYNC_ENABLED", "true")
    lifecycle = SimpleNamespace(acquire=Mock(), ensure_healthy=Mock(), close=Mock())
    monkeypatch.setattr(workers, "WorkerLifecycle", lambda _url: lifecycle)
    monkeypatch.setattr(workers, "assert_active", lambda _url: None)
    monkeypatch.setattr(workers, "enabled_securities", lambda _url: [{"stock_code": "AAPL"}])
    collect = Mock(return_value=22)
    monkeypatch.setattr(workers, "collect_security", collect)
    result = workers.run_once("unused", "quotes")
    assert result["kind"] == "quotes"
    collect.assert_called_once()
    lifecycle.close.assert_called_once()
    lifecycle.acquire.reset_mock()
    for kind in ("announcements", "news_sync"):
        assert workers.run_once("unused", kind) == {"status": "disabled", "kind": kind}
        assert workers._run_once("unused", kind, lifecycle) == {"status": "disabled", "kind": kind}
    lifecycle.acquire.assert_not_called()


def test_collection_dispatch_honors_independent_switch(monkeypatch):
    monkeypatch.setenv("HL_APP_COLLECTORS_ENABLED", "false")
    monkeypatch.setenv("HL_APP_QUOTES_ENABLED", "true")
    monkeypatch.setattr(workers, "assert_active", lambda _url: None)
    targets = Mock(return_value=[{"stock_code": "AAPL"}])
    monkeypatch.setattr(workers, "enabled_securities", targets)
    collect = Mock(return_value=20)
    monkeypatch.setattr(workers, "collect_security", collect)
    lifecycle = SimpleNamespace(ensure_healthy=Mock())
    assert workers._run_once("unused", "quotes", lifecycle)["kind"] == "quotes"
    targets.assert_called_once_with("unused")
    collect.assert_called_once()


def test_quotes_disable_and_invalid_switch_do_not_acquire_worker_lock(monkeypatch):
    monkeypatch.setenv("HL_APP_COLLECTORS_ENABLED", "true")
    lifecycle = Mock(side_effect=AssertionError("must not acquire lock"))
    monkeypatch.setattr(workers, "WorkerLifecycle", lifecycle)
    monkeypatch.setenv("HL_APP_QUOTES_ENABLED", "false")
    assert workers.run_once("unused", "quotes")["status"] == "disabled"
    monkeypatch.setenv("HL_APP_QUOTES_ENABLED", "yes")
    with pytest.raises(workers.ConfigError, match="HL_APP_QUOTES_ENABLED"):
        workers.run_once("unused", "quotes")
    lifecycle.assert_not_called()
