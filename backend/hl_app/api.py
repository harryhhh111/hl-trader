from __future__ import annotations

import os
import signal
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import AuthService
from .config import Settings
from .db import WebLifecycle
from .routes import hyperliquid, news, session, watchlist


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    lifecycle = WebLifecycle(settings.database_url)
    lifecycle.acquire()
    app.state.settings = settings
    app.state.web_lifecycle = lifecycle
    app.state.auth = AuthService(
        settings.database_url, settings.auth_token, settings.session_ttl_seconds
    )
    shutdown_started = False

    def initiate_shutdown() -> None:
        nonlocal shutdown_started
        if shutdown_started:
            return
        shutdown_started = True
        hyperliquid.pairs_service.terminate_collectors()
        timer = threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.daemon = True
        timer.start()

    app.state.initiate_shutdown = initiate_shutdown
    try:
        app.state.auth.verify_configuration()
        yield
    finally:
        lifecycle.close()


def create_app() -> FastAPI:
    app = FastAPI(title="hl-app", lifespan=lifespan)

    @app.middleware("http")
    async def trusted_origin(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin", "").rstrip("/")
            if origin not in request.app.state.settings.trusted_origins:
                return JSONResponse(status_code=403, content={"detail": "Origin 不受信任"})
        return await call_next(request)

    app.include_router(session.router, prefix="/api/v1")
    app.include_router(hyperliquid.router, prefix="/api/v1")
    app.include_router(watchlist.router, prefix="/api/v1")
    app.include_router(news.router, prefix="/api/v1")

    @app.get("/api/v1/health")
    def health() -> dict[str, dict[str, str]]:
        return {"data": {"status": "ok"}}

    return app


app = create_app()
