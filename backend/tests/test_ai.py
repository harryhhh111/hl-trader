from __future__ import annotations

import json
import threading

import pytest

from hl_app.services import ai


def test_media_and_official_enhancement_contract(monkeypatch) -> None:
    monkeypatch.setattr(ai, "_run_mmx", lambda *_args: json.dumps({"title_zh": "标题", "summary_zh": "摘要", "importance": "high"}))
    assert ai.enhance_media("Title", "Summary", "MU") == {"title_zh": "标题", "summary_zh": "摘要", "importance": "high"}
    monkeypatch.setattr(ai, "_run_mmx", lambda *_args: '{"importance":"normal"}')
    assert ai.enhance_official("8-K", "8-K", "MU") == {"importance": "normal"}


@pytest.mark.parametrize("payload", ["not-json", '{"title_zh":"x"}', '{"title_zh":"x","summary_zh":"y","importance":"urgent"}'])
def test_invalid_model_output_fails_without_defaulting(monkeypatch, payload) -> None:
    monkeypatch.setattr(ai, "_run_mmx", lambda *_args: payload)
    with pytest.raises(ai.AIError):
        ai.enhance_media("t", "", "MU")


def test_mmx_requires_explicit_configuration(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HL_APP_MMX_BIN", raising=False)
    monkeypatch.delenv("HL_APP_MMX_MODEL", raising=False)
    with pytest.raises(ai.AIError, match="显式配置"):
        ai._run_mmx("s", "u")
    script = tmp_path / "mmx"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setenv("HL_APP_MMX_BIN", str(script))
    monkeypatch.setenv("HL_APP_MMX_MODEL", "fixture-model")
    with pytest.raises(ai.AIError, match="空输出"):
        ai._run_mmx("s", "u")


def test_news_pool_allows_only_one_call(monkeypatch) -> None:
    active = 0
    maximum = 0
    guard = threading.Lock()
    calls = 0

    def slow(*_args):
        nonlocal active, maximum, calls
        with guard:
            active += 1
            calls += 1
            maximum = max(maximum, active)
        threading.Event().wait(0.05)
        with guard:
            active -= 1
        return '{"title_zh":"x","summary_zh":"y","importance":"normal"}'

    monkeypatch.setattr(ai, "_run_mmx", slow)
    threads = [threading.Thread(target=ai.enhance_media, args=("t", "", "MU")) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == 4 and maximum == 1
