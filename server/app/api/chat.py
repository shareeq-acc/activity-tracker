from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin import get_setting
from app.core.config import settings
from app.core.database import get_session
from app.models.meta import ChatMessage
from app.schemas import ChatIn
from app.services import analytics as an
from app.services import llm

router = APIRouter(prefix="/chat", tags=["chat"])

HISTORY_TURNS = 12


@router.get("")
async def get_history(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (
        (
            await session.execute(
                select(ChatMessage).order_by(ChatMessage.id.desc()).limit(HISTORY_TURNS * 2)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()
    return {
        "messages": [
            {
                "role": r.role,
                "content": r.content,
                "provider": r.provider,
                "at": an.to_local(r.created_at).isoformat(),
            }
            for r in rows
        ]
    }


@router.delete("")
async def clear_history(session: AsyncSession = Depends(get_session)) -> dict:
    await session.execute(delete(ChatMessage))
    await session.commit()
    return {"cleared": True}


@router.post("")
async def ask(payload: ChatIn, session: AsyncSession = Depends(get_session)) -> dict:
    if payload.reset:
        await session.execute(delete(ChatMessage))
        await session.commit()

    provider = payload.provider or await get_setting(
        session, "llm_provider", settings.default_llm_provider
    )
    if provider not in llm.PROVIDERS:
        provider = settings.default_llm_provider

    rows = (
        (
            await session.execute(
                select(ChatMessage).order_by(ChatMessage.id.desc()).limit(HISTORY_TURNS * 2)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()
    history = [{"role": r.role, "content": r.content} for r in rows]

    try:
        result = await llm.chat(session, provider, history, payload.message)
    except Exception as exc:  # noqa: BLE001 - shown to the user verbatim
        raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc

    session.add(ChatMessage(role="user", content=payload.message, provider=provider))
    session.add(
        ChatMessage(role="assistant", content=result["reply"], provider=provider)
    )
    await session.commit()

    return {
        "reply": result["reply"],
        "provider": provider,
        "tools_used": result.get("tools_used", []),
    }


@router.get("/models")
async def models() -> dict:
    """Which local models are actually pulled, for the settings panel."""
    reachable, installed = await llm.ollama.list_models()
    return {
        "ollama_reachable": reachable,
        "ollama_models": installed,
        "ollama_model_ready": settings.ollama_model in installed,
        "configured": {
            "ollama": settings.ollama_model,
            "gemini": settings.gemini_model,
        },
    }
