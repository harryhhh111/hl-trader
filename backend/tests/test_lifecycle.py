from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hl_app.auth import require_session
from hl_app.db import WebLifecycleLost
from hl_app.services import hyperliquid_pairs


def test_lock_loss_rejects_data_kills_collectors_and_starts_shutdown(monkeypatch) -> None:
    events = []

    class LostLifecycle:
        def ensure_healthy(self):
            raise WebLifecycleLost("lost")

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                web_lifecycle=LostLifecycle(),
                initiate_shutdown=lambda: events.append("shutdown"),
            )
        ),
        cookies={},
        method="GET",
        state=SimpleNamespace(),
    )
    monkeypatch.setattr(hyperliquid_pairs, "terminate_collectors", lambda: events.append("kill"))
    with pytest.raises(HTTPException) as error:
        require_session(request)
    assert error.value.status_code == 503
    assert events == ["kill", "shutdown"]
