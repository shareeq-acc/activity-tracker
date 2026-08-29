from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.models.focus import FocusSession
from app.schemas import FocusPatchIn, FocusStartIn
from app.services import analytics as an
from app.services.categorizer import ruleset

router = APIRouter(prefix="/focus", tags=["focus"])


def elapsed_seconds(fs: FocusSession, now: datetime | None = None) -> float:
    """Authoritative elapsed time.

    Derived from stored anchors rather than a client-side counter, so a page
    reload, a closed laptop, or a container restart cannot lose or invent time.
    """
    now = now or datetime.now(tz=timezone.utc)
    total = fs.accumulated_s
    if fs.status == "running" and fs.last_resumed_at is not None:
        total += (now - fs.last_resumed_at).total_seconds()
    return max(0.0, total)


def serialize(fs: FocusSession) -> dict:
    rs = ruleset.get()
    cat = rs.categories.get(fs.category)
    elapsed = elapsed_seconds(fs)
    remaining = None
    if fs.kind == "timer" and fs.planned_s:
        remaining = max(0.0, fs.planned_s - elapsed)
    return {
        "id": fs.id,
        "kind": fs.kind,
        "label": fs.label,
        "category": fs.category,
        "category_label": cat.label if cat else fs.category,
        "color": cat.color if cat else "#64748b",
        "planned_s": fs.planned_s,
        "status": fs.status,
        "started_at": an.to_local(fs.started_at).isoformat(),
        "ended_at": an.to_local(fs.ended_at).isoformat() if fs.ended_at else None,
        "elapsed_s": round(elapsed, 1),
        "remaining_s": round(remaining, 1) if remaining is not None else None,
        "notes": fs.notes,
    }


async def _get(session: AsyncSession, focus_id: int) -> FocusSession:
    fs = await session.get(FocusSession, focus_id)
    if fs is None:
        raise HTTPException(404, "No such session")
    return fs


async def _autocomplete(session: AsyncSession, fs: FocusSession) -> FocusSession:
    """A countdown that has run out is finished, even if nobody was watching."""
    if fs.kind == "timer" and fs.status == "running" and fs.planned_s:
        elapsed = elapsed_seconds(fs)
        if elapsed >= fs.planned_s:
            overshoot = elapsed - fs.planned_s
            fs.status = "completed"
            fs.accumulated_s = float(fs.planned_s)
            # Back-date the end to when the countdown actually hit zero, not to
            # whenever the browser next asked.
            fs.ended_at = datetime.now(tz=timezone.utc) - timedelta(seconds=overshoot)
            fs.last_resumed_at = None
            await session.commit()
    return fs


@router.post("")
async def start(
    payload: FocusStartIn, session: AsyncSession = Depends(get_session)
) -> dict:
    if payload.kind == "timer" and not payload.planned_s:
        raise HTTPException(400, "A timer needs planned_s")

    # Only one manual session at a time; starting a new one closes the old.
    running = (
        (
            await session.execute(
                select(FocusSession).where(FocusSession.status.in_(["running", "paused"]))
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(tz=timezone.utc)
    for old in running:
        old.accumulated_s = elapsed_seconds(old, now)
        old.status = "completed"
        old.ended_at = now
        old.last_resumed_at = None

    fs = FocusSession(
        kind=payload.kind,
        label=payload.label,
        category=payload.category,
        planned_s=payload.planned_s,
        status="running",
        started_at=now,
        accumulated_s=0.0,
        last_resumed_at=now,
    )
    session.add(fs)
    await session.commit()
    await session.refresh(fs)
    return serialize(fs)


@router.get("/active")
async def active(session: AsyncSession = Depends(get_session)) -> dict:
    stmt = (
        select(FocusSession)
        .where(FocusSession.status.in_(["running", "paused"]))
        .order_by(FocusSession.started_at.desc())
        .limit(1)
    )
    fs = (await session.execute(stmt)).scalars().first()
    if fs is None:
        return {"active": None}
    fs = await _autocomplete(session, fs)
    return {"active": serialize(fs) if fs.status in ("running", "paused") else None,
            "just_completed": serialize(fs) if fs.status == "completed" else None}


@router.post("/{focus_id}/pause")
async def pause(focus_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    fs = await _get(session, focus_id)
    if fs.status != "running":
        raise HTTPException(409, f"Session is {fs.status}, not running")
    now = datetime.now(tz=timezone.utc)
    fs.accumulated_s = elapsed_seconds(fs, now)
    fs.last_resumed_at = None
    fs.status = "paused"
    await session.commit()
    return serialize(fs)


@router.post("/{focus_id}/resume")
async def resume(focus_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    fs = await _get(session, focus_id)
    if fs.status != "paused":
        raise HTTPException(409, f"Session is {fs.status}, not paused")
    fs.last_resumed_at = datetime.now(tz=timezone.utc)
    fs.status = "running"
    await session.commit()
    return serialize(fs)


@router.post("/{focus_id}/stop")
async def stop(focus_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    fs = await _get(session, focus_id)
    if fs.status in ("completed", "cancelled"):
        return serialize(fs)
    now = datetime.now(tz=timezone.utc)
    fs.accumulated_s = elapsed_seconds(fs, now)
    fs.last_resumed_at = None
    fs.status = "completed"
    fs.ended_at = now
    await session.commit()
    return serialize(fs)


@router.post("/{focus_id}/cancel")
async def cancel(focus_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    fs = await _get(session, focus_id)
    now = datetime.now(tz=timezone.utc)
    fs.accumulated_s = elapsed_seconds(fs, now)
    fs.last_resumed_at = None
    fs.status = "cancelled"
    fs.ended_at = now
    await session.commit()
    return serialize(fs)


@router.patch("/{focus_id}")
async def patch(
    focus_id: int, payload: FocusPatchIn, session: AsyncSession = Depends(get_session)
) -> dict:
    fs = await _get(session, focus_id)
    if payload.label is not None:
        fs.label = payload.label
    if payload.category is not None:
        fs.category = payload.category
    if payload.notes is not None:
        fs.notes = payload.notes
    await session.commit()
    return serialize(fs)


@router.delete("/{focus_id}")
async def remove(focus_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    fs = await _get(session, focus_id)
    await session.delete(fs)
    await session.commit()
    return {"deleted": focus_id}


@router.get("")
async def history(
    period: str = Query("7d"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    start, end, label = an.resolve_period(period)
    stmt = (
        select(FocusSession)
        .where(FocusSession.started_at >= start, FocusSession.started_at < end)
        .order_by(FocusSession.started_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()

    by_label: dict[str, float] = defaultdict(float)
    by_category: dict[str, float] = defaultdict(float)
    for fs in rows:
        if fs.status == "cancelled":
            continue
        secs = elapsed_seconds(fs)
        by_label[fs.label or "(unlabelled)"] += secs
        by_category[fs.category] += secs

    rs = ruleset.get()
    return {
        "period": period,
        "period_label": label,
        "sessions": [serialize(fs) for fs in rows],
        "total_seconds": round(sum(by_category.values())),
        "by_label": sorted(
            ({"label": k, "seconds": round(v)} for k, v in by_label.items()),
            key=lambda d: -d["seconds"],
        ),
        "by_category": sorted(
            (
                {
                    "category": k,
                    "label": rs.categories[k].label if k in rs.categories else k.title(),
                    "color": rs.categories[k].color if k in rs.categories else "#64748b",
                    "seconds": round(v),
                }
                for k, v in by_category.items()
            ),
            key=lambda d: -d["seconds"],
        ),
    }
