from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.models.activity import Segment
from app.models.meta import Goal, Setting
from app.schemas import GoalIn
from app.services import analytics as an
from app.services import classifier
from app.services.categorizer import ruleset

router = APIRouter(tags=["admin"])


# --- rules -----------------------------------------------------------------


@router.get("/rules")
async def get_rules(session: AsyncSession = Depends(get_session)) -> dict:
    rs = ruleset.get()

    # Apps that fell through every rule, ranked by how much time they cost.
    stmt = (
        select(Segment.exe, func.sum(Segment.duration_s), func.count())
        .where(Segment.category == rs.fallback)
        .group_by(Segment.exe)
        .order_by(func.sum(Segment.duration_s).desc())
        .limit(30)
    )
    rows = (await session.execute(stmt)).all()

    return {
        "path": str(settings.rules_path),
        "error": ruleset.error,
        "categories": [
            {"key": c.key, "label": c.label, "bucket": c.bucket, "color": c.color}
            for c in rs.categories.values()
        ],
        "rules": [
            {
                "id": r.id,
                "category": r.category,
                "exe": list(r.exe),
                "title_any": list(r.title_any),
                "title_all": list(r.title_all),
                "title_not": list(r.title_not),
            }
            for r in rs.rules
        ],
        "uncategorized": [
            {"exe": exe, "app": an.app_name(exe), "seconds": round(secs or 0), "count": n}
            for exe, secs, n in rows
        ],
    }


@router.post("/rules/reload")
async def reload_rules(session: AsyncSession = Depends(get_session)) -> dict:
    """Re-read rules.yml and re-categorise all history.

    Fixing a rule should fix the past too, otherwise old data stays wrong
    forever and the totals never agree with themselves.
    """
    rs = ruleset.reload()
    if ruleset.error:
        raise HTTPException(400, f"rules.yml failed to load: {ruleset.error}")

    changed = await classifier.reapply(session)

    return {
        "reloaded": True,
        "categories": len(rs.categories),
        "rules": len(rs.rules),
        "segments_recategorized": changed,
    }


# --- goals -----------------------------------------------------------------


@router.get("/goals")
async def list_goals(session: AsyncSession = Depends(get_session)) -> dict:
    from app.services.insights import goals_progress

    return {"goals": await goals_progress(session)}


@router.post("/goals")
async def upsert_goal(payload: GoalIn, session: AsyncSession = Depends(get_session)) -> dict:
    stmt = select(Goal).where(
        Goal.scope == payload.scope, Goal.target_key == payload.target_key
    )
    goal = (await session.execute(stmt)).scalars().first()
    if goal is None:
        goal = Goal(scope=payload.scope, target_key=payload.target_key)
        session.add(goal)
    goal.weekly_target_hours = payload.weekly_target_hours
    goal.active = payload.weekly_target_hours > 0
    await session.commit()
    return {"ok": True, "id": goal.id}


@router.delete("/goals/{goal_id}")
async def delete_goal(goal_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    goal = await session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(404, "No such goal")
    await session.delete(goal)
    await session.commit()
    return {"deleted": goal_id}


# --- settings --------------------------------------------------------------


async def get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    row = await session.get(Setting, key)
    return row.value if row else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.commit()


@router.get("/settings")
async def read_settings(session: AsyncSession = Depends(get_session)) -> dict:
    from app.services.llm import provider_status

    return {
        "timezone": settings.tz,
        "llm_provider": await get_setting(
            session, "llm_provider", settings.default_llm_provider
        ),
        "providers": provider_status(),
    }


@router.put("/settings/llm_provider")
async def set_provider(
    payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    value = str(payload.get("provider", "")).strip()
    if value not in ("gemini", "ollama"):
        raise HTTPException(400, "provider must be 'gemini' or 'ollama'")
    await set_setting(session, "llm_provider", value)
    return {"llm_provider": value}


# --- housekeeping ----------------------------------------------------------


@router.delete("/segments")
async def delete_segments(
    host: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Delete every segment recorded under one host label.

    Exists so seeded demo data can be removed cleanly without touching real
    history. `host` must match exactly - there is no wildcard, deliberately.
    """
    if not host.strip():
        raise HTTPException(400, "host is required and cannot be blank")
    result = await session.execute(
        Segment.__table__.delete().where(Segment.host == host)
    )
    await session.commit()
    return {"deleted": result.rowcount or 0, "host": host}


@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)) -> dict:
    total, first, last = (
        await session.execute(
            select(func.count(Segment.id), func.min(Segment.started_at), func.max(Segment.ended_at))
        )
    ).one()
    return {
        "segments": total or 0,
        "first_seen": an.to_local(first).isoformat() if first else None,
        "last_seen": an.to_local(last).isoformat() if last else None,
        "db_path": str(settings.db_path),
        "db_bytes": settings.db_path.stat().st_size if settings.db_path.exists() else 0,
    }
