from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session
from app.services.insights import build_report

router = APIRouter(tags=["insights"])


@router.get("/insights")
async def insights(
    period: str = Query("7d"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await build_report(session, period)
