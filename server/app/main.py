from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import activity, admin, chat, classify, focus, ingest, insights
from app.core.config import settings
from app.core.database import init_db
from app.services.categorizer import ruleset

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await init_db()
    ruleset.reload()
    yield


app = FastAPI(
    title="Activity Tracker",
    description="Personal time tracking, insights, and a chat interface over your own data.",
    version="1.0.0",
    lifespan=lifespan,
)

for router in (
    ingest.router, activity.router, insights.router,
    focus.router, admin.router, chat.router, classify.router,
):
    app.include_router(router, prefix="/api")


@app.middleware("http")
async def no_store_api(request, call_next):
    """Never let the browser cache an API response.

    These are all live counters. Without this the browser will happily serve a
    stale /api/summary from memory cache while /api/live comes back fresh, and
    the dashboard shows two different totals for the same minute.
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict:
    return {
        "ok": True,
        "rules_error": ruleset.error or None,
        "timezone": settings.tz,
    }


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
