"""FastAPI application entry point.

Run with: uvicorn server.app:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from server import storage
from server.config import get_settings
from server.db import close_db, init_db
from server.rate_limit import limiter
from server.routers import (
    admin,
    baselines,
    batch,
    change_requests,
    contributors,
    current,
    me,
    timetable,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await init_db()
    try:
        yield
    finally:
        await close_db()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="MLSC Timetable API",
        version="0.2.0",
        description="Backend for the MLSC timetable site. See BACKEND_PLAN.md.",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-User-Id"],
    )

    # Rate limiting (slowapi). The limiter instance lives in server.rate_limit
    # so individual routers can decorate handlers with @limiter.limit(...).
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)

    @app.exception_handler(RateLimitExceeded)
    async def _handle_rate_limit(_: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={
                "error": f"Rate limit exceeded: {exc.detail}",
                "code": "rate_limited",
            },
        )

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        return {"ok": True}

    @app.exception_handler(storage.DataMissing)
    async def _handle_missing(_: Request, exc: storage.DataMissing) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"error": str(exc), "code": "data_missing"},
        )

    app.include_router(batch.router)
    app.include_router(current.router)
    app.include_router(timetable.router)
    app.include_router(me.router)
    app.include_router(baselines.router)
    app.include_router(contributors.router)
    app.include_router(change_requests.router)
    app.include_router(change_requests.admin_router)
    app.include_router(admin.router)

    return app


app = create_app()
