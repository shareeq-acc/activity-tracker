from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, utcnow


class TitleClassification(Base):
    """A decided category for one specific (app, window title) pair.

    Keyword rules cannot cover the long tail of browser tabs: every YouTube
    video is the same domain, so only the title separates a tutorial from a
    gaming stream. Anything the rules cannot resolve is classified once by the
    LLM and cached here forever, keyed on the exact title.

    `source` matters:
      llm    - decided by the model, re-decidable if rules or prompts change
      manual - you overrode it in the UI; never re-decided automatically
    """

    __tablename__ = "title_classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exe: Mapped[str] = mapped_column(String(255), index=True)
    title_key: Mapped[str] = mapped_column(String(400), index=True)
    title: Mapped[str] = mapped_column(Text, default="")

    category: Mapped[str] = mapped_column(String(64), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(16), default="llm", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index("ix_title_class_lookup", "exe", "title_key", unique=True),
    )
