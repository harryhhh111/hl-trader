from __future__ import annotations

import json
from pathlib import Path

import pytest

from hl_app import workers
from hl_app.services import news_sync

FIXTURE = Path(__file__).parents[2] / "tests/fixtures/integrations/hl_trader_news_sync_v1.json"


def fixture_bytes() -> bytes:
    return FIXTURE.read_bytes()


def test_sync_fixture_preserves_chinese_null_time_and_multiple_relations() -> None:
    response = news_sync.parse_response(fixture_bytes())
    assert response.source_instance == "stock-data-fixture"
    assert response.sources[0].allowed_domain == "example.com"
    assert response.sources[0].source_status == "blocked"
    assert response.items[0].published_at is None
    assert response.items[0].revision == 2
    assert len(response.items[-1].relations) == 2
    assert response.items[-1].title_zh == "共享测试资讯"


def test_unknown_dto_field_and_invalid_json_are_rejected() -> None:
    value = json.loads(fixture_bytes())
    value["unexpected"] = True
    with pytest.raises(news_sync.NewsSyncError, match="严格 DTO"):
        news_sync.parse_response(json.dumps(value).encode())
    with pytest.raises(news_sync.NewsSyncError, match="严格 DTO"):
        news_sync.parse_response(b"not-json")


class Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, _limit: int) -> bytes:
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class Opener:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return Response(self.payload)


def test_client_uses_fixed_https_path_and_bounded_response() -> None:
    opener = Opener(fixture_bytes())
    client = news_sync.NewsSyncClient("https://stock-data.example.com", "stock-data-fixture", "x" * 32, opener=opener)
    response = client.fetch("opaque cursor")
    assert response.next_cursor == "opaque-cursor-4"
    assert opener.request.full_url.startswith("https://stock-data.example.com/api/v1/integrations/hl-trader/news/sync?")
    assert "cursor=opaque+cursor" in opener.request.full_url
    with pytest.raises(news_sync.NewsSyncError, match="HTTPS"):
        news_sync.NewsSyncClient("http://stock-data.example.com", "source", "x" * 32)
    with pytest.raises(news_sync.NewsSyncError, match="长度不足"):
        news_sync.NewsSyncClient("https://stock-data.example.com", "source", "short")
    oversized = news_sync.NewsSyncClient(
        "https://stock-data.example.com", "stock-data-fixture", "x" * 32,
        opener=Opener(b"x" * (news_sync.MAX_RESPONSE_BYTES + 1)),
    )
    with pytest.raises(news_sync.NewsSyncError, match="2 MiB"):
        oversized.fetch(None)


def test_sync_worker_requires_explicit_enable_and_no_local_enhancement(monkeypatch) -> None:
    monkeypatch.setenv("HL_APP_COLLECTORS_ENABLED", "true")
    monkeypatch.setenv("HL_APP_NEWS_SYNC_ENABLED", "false")
    lifecycle = type("Lifecycle", (), {"ensure_healthy": lambda self: None})()
    assert workers._run_once("postgresql://runtime", "news_sync", lifecycle)["status"] == "disabled"
    monkeypatch.setenv("HL_APP_NEWS_SYNC_ENABLED", "true")
    monkeypatch.setenv("HL_APP_ENHANCEMENT_ENABLED", "true")
    with pytest.raises(workers.ConfigError, match="ENHANCEMENT_ENABLED=false"):
        workers._run_once("postgresql://runtime", "news_sync", lifecycle)
