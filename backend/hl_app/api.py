from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from .config import Settings
from .db import WebLifecycle


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    lifecycle = WebLifecycle(settings.database_url)
    lifecycle.acquire()
    app.state.settings = settings
    app.state.web_lifecycle = lifecycle
    try:
        yield
    finally:
        lifecycle.close()


def create_app() -> FastAPI:
    app = FastAPI(title="hl-app", lifespan=lifespan)

    @app.get("/api/v1/health")
    def health() -> dict[str, dict[str, str]]:
        return {"data": {"status": "ok"}}

    return app


app = create_app()
