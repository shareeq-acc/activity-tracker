from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models.activity import Segment
from app.services import analytics as an
from app.services.categorizer import ruleset

router = APIRouter(tags=["activity"])


@router.get("/summary")
async def summary(
    period: str = Query("today"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    start, end, label = an.resolve_period(period)
    slices = await an.load_slices(session, start, end)
    active = [s for s in slices if not s.is_idle]
    return {
        "period": period,
        "period_label": label,
        "start": an.to_local(start).isoformat(),
        "end": an.to_local(end).isoformat(),
        "ratio": an.ratio(an.totals_by_bucket(slices)),
        "categories": an.totals_by_category(active),
        "top_apps": an.top_apps(active, limit=10),
        "top_titles": an.top_titles(active, limit=12),
        "daily": an.daily_series(slices),
        "hourly": an.hourly_profile(slices),
        "fragmentation": an.fragmentation(slices),
    }


@router.get("/timeline")
async def timeline(
    day: str | None = Query(None, description="local date, YYYY-MM-DD; defaults to today"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    target = date.fromisoformat(day) if day else an.today_local()
    start, end = an.day_bounds(target)
    slices = await an.load_slices(session, start, end)

    rs = ruleset.get()
    day_start_local = an.to_local(start)
    rows = []
    for sl in slices:
        offset = (sl.start - start).total_seconds()
        cat = rs.categories.get(sl.category)
        rows.append(
            {
                "app": an.app_name(sl.exe),
                "exe": sl.exe,
                "title": sl.title,
                "category": sl.category,
                "label": cat.label if cat else sl.category,
                "color": cat.color if cat else "#64748b",
                "bucket": sl.bucket,
                "start": an.to_local(sl.start).isoformat(),
                "end": an.to_local(sl.end).isoformat(),
                "offset_s": round(offset),
                "seconds": round(sl.seconds),
            }
        )
    return {
        "date": target.isoformat(),
        "day_start": day_start_local.isoformat(),
        "segments": rows,
    }


@router.get("/live")
async def live(session: AsyncSession = Depends(get_session)) -> dict:
    """What is happening right now, plus whether the collector is alive."""
    stmt = select(Segment).order_by(Segment.ended_at.desc()).limit(1)
    latest = (await session.execute(stmt)).scalars().first()

    now = datetime.now(tz=timezone.utc)
    current = None
    connected = False
    if latest is not None:
        age = (now - latest.ended_at).total_seconds()
        # The collector flushes every 30s; allow generous slack before we call
        # it offline. A negative age means the newest row is dated in the
        # future (clock skew), which is not evidence of a live collector.
        connected = 0 <= age < 120
        rs = ruleset.get()
        cat = rs.categories.get(latest.category)
        current = {
            "app": an.app_name(latest.exe),
            "exe": latest.exe,
            "title": latest.title,
            "category": latest.category,
            "label": cat.label if cat else latest.category,
            "color": cat.color if cat else "#64748b",
            "bucket": latest.bucket,
            "since": an.to_local(latest.started_at).isoformat(),
            "seconds": round((latest.ended_at - latest.started_at).total_seconds()),
            "last_seen_s": max(0, round(age)),
        }

    start, end, _ = an.resolve_period("today")
    slices = await an.load_slices(session, start, end)
    return {
        "connected": connected,
        "current": current,
        "today": an.ratio(an.totals_by_bucket(slices)),
        "server_time": an.to_local(now).isoformat(),
    }


@router.get("/segments")
async def segments(
    start: datetime | None = None,
    end: datetime | None = None,
    q: str | None = Query(None, description="substring match on window title or app"),
    limit: int = Query(200, ge=1, le=2000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if end is None:
        end = datetime.now(tz=timezone.utc)
    if start is None:
        start = end - timedelta(days=7)

    stmt = select(Segment).where(Segment.ended_at > start, Segment.started_at < end)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            func.lower(Segment.title).like(like) | func.lower(Segment.exe).like(like)
        )
    stmt = stmt.order_by(Segment.started_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()

    return {
        "count": len(rows),
        "segments": [
            {
                "app": an.app_name(r.exe),
                "title": r.title,
                "category": r.category,
                "bucket": r.bucket,
                "start": an.to_local(r.started_at).isoformat(),
                "end": an.to_local(r.ended_at).isoformat(),
                "seconds": round(r.duration_s),
            }
            for r in rows
        ],
    }


@router.get("/days")
async def days(
    limit: int = Query(60, ge=1, le=400),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Per-day totals for the history heatmap."""
    end = datetime.now(tz=timezone.utc)
    start, _ = an.day_bounds(an.today_local() - timedelta(days=limit - 1))
    slices = await an.load_slices(session, start, end)
    return {"days": an.daily_series(slices)}
