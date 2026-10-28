from __future__ import annotations

from hl_app.api import create_app


def test_app_factory_does_not_require_database_at_import_time() -> None:
    app = create_app()
    paths = {path for route in app.routes if (path := getattr(route, "path", None)) is not None}
    assert "/api/v1/health" in paths
    assert not any(path.startswith("/api/us") or path.startswith("/api/cn") for path in paths)
