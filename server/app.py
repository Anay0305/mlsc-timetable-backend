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
    analytics,
    announcements,
    baselines,
    batch,
    calendar,
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
    # First-boot seed: lift assets/subjects.json into the `subjects`
    # collection if it's empty. Idempotent — does nothing if any rows exist.
    try:
        from timetable_parser.core.subject_catalog import (
            ensure_catalog,
            seed_subjects_from_file_if_empty,
        )

        seeded = await seed_subjects_from_file_if_empty()
        if seeded:
            logging.getLogger("server.app").info(
                "Seeded %d subject(s) from assets/subjects.json", seeded
            )
        await ensure_catalog()
    except Exception:
        logging.getLogger("server.app").exception("Subject catalog bootstrap failed")
    # Start the Google Calendar background worker (no-op if not configured)
    try:
        from server.calendar_sync import start_worker
        start_worker()
    except Exception:
        logging.getLogger("server.app").exception("Calendar worker failed to start")
    try:
        yield
    finally:
        from server.calendar_sync import stop_worker
        stop_worker()
        await close_db()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="MLSC Timetable API",
        version="0.2.0",
        description="Backend for the MLSC timetable site. See BACKEND_PLAN.md.",
        lifespan=_lifespan,
    )

    # Rate limiting (slowapi). The limiter instance lives in server.rate_limit
    # so individual routers can decorate handlers with @limiter.limit(...).
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)

    # Keep CORS outside the rate-limit middleware so even error responses from
    # the API retain CORS headers. Otherwise browsers hide the real 500 and
    # report it as a misleading cross-origin failure.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-User-Id", "X-User-Email", "*"],
    )

    @app.exception_handler(RateLimitExceeded)
    async def _handle_rate_limit(_: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={
                "error": f"Rate limit exceeded: {exc.detail}",
                "code": "rate_limited",
            },
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        # Convert route-level crashes into normal responses so the outer CORS
        # middleware can attach headers and clients can see a real 500 instead
        # of a misleading browser-level CORS error.
        logging.getLogger("server.app").exception("Unhandled API error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "code": "internal_error",
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
    app.include_router(announcements.router)
    app.include_router(change_requests.router)
    app.include_router(change_requests.admin_router)
    app.include_router(admin.router)
    app.include_router(calendar.router)
    app.include_router(analytics.router)
    app.include_router(analytics.admin_router)

    return app


app = create_app()
