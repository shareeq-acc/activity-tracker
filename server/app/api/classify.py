from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import get_setting
from app.core.config import settings
from app.core.database import get_session
from app.models.classification import TitleClassification
from app.services import analytics as an
from app.services import classifier
from app.services.categorizer import ruleset

router = APIRouter(prefix="/classify", tags=["classify"])


class OverrideIn(BaseModel):
    exe: str = Field(max_length=255)
    title: str = Field(default="", max_length=400)
    category: str = Field(max_length=64)


@router.get("/pending")
async def pending(
    limit: int = Query(40, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Windows the rules could not resolve, ranked by time cost."""
    items = await classifier.pending_titles(session, limit=limit)
    return {
        "count": len(items),
        "pending": [
            {**it, "app": an.app_name(it["exe"]), "time": _hm(it["seconds"])}
            for it in items
        ],
    }


@router.post("/run")
async def run(
    limit: int = Query(20, ge=1, le=100),
    provider: str | None = Query(None, pattern="^(gemini|ollama)$"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    chosen = provider or await get_setting(
        session, "llm_provider", settings.default_llm_provider
    )
    result = await classifier.classify(session, chosen, limit=limit)
    result["provider"] = chosen
    rs = ruleset.get()
    for r in result.get("results", []):
        cat = rs.categories.get(r["category"])
        r["app"] = an.app_name(r["exe"])
        r["label"] = cat.label if cat else r["category"]
        r["color"] = cat.color if cat else "#64748b"
    return result


@router.get("")
async def listing(
    limit: int = Query(200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        (
            await session.execute(
                select(TitleClassification)
                .order_by(TitleClassification.updated_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    rs = ruleset.get()
    return {
        "count": len(rows),
        "classifications": [
            {
                "id": r.id,
                "exe": r.exe,
                "app": an.app_name(r.exe),
                "title": r.title,
                "category": r.category,
                "label": rs.categories[r.category].label
                if r.category in rs.categories
                else r.category,
                "color": rs.categories[r.category].color
                if r.category in rs.categories
                else "#64748b",
                "source": r.source,
                "confidence": r.confidence,
            }
            for r in rows
        ],
    }


@router.put("/override")
async def override(
    payload: OverrideIn, session: AsyncSession = Depends(get_session)
) -> dict:
    """Pin a window to a category by hand. Never re-decided automatically."""
    rs = ruleset.get()
    if payload.category not in rs.categories:
        raise HTTPException(400, f"Unknown category '{payload.category}'")
    await classifier.upsert(
        session, payload.exe, payload.title, payload.category, source="manual", confidence=1.0
    )
    await session.commit()
    updated = await classifier.reapply(session)
    return {"ok": True, "segments_updated": updated}


@router.delete("/{classification_id}")
async def remove(
    classification_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    row = await session.get(TitleClassification, classification_id)
    if row is None:
        raise HTTPException(404, "No such classification")
    await session.delete(row)
    await session.commit()
    classifier.cache.clear()
    await classifier.cache.load(session)
    updated = await classifier.reapply(session)
    return {"deleted": classification_id, "segments_updated": updated}


def _hm(seconds: float) -> str:
    seconds = int(seconds)
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"
