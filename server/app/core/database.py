from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.base import Base
# Imported for their side effect: every model must be registered on
# Base.metadata before create_all runs, or its table is silently skipped.
from app.models import activity, classification, focus, meta  # noqa: F401

settings.data_dir.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(settings.db_url, echo=False, future=True)


@event.listens_for(engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    """WAL keeps the dashboard readable while the collector is writing."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Cheap forward-compatible migration: add columns that later versions
        # introduce, ignoring the error when they already exist.
        for stmt in ():
            try:
                await conn.execute(text(stmt))
            except Exception:  # noqa: BLE001 - column already present
                pass
