from __future__ import annotations

from datetime import datetime

from sqlalchemy import Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, utcnow


class FocusSession(Base):
    """A manually driven timer or stopwatch run.

    Elapsed time is never stored as a running total that the client keeps
    ticking. `accumulated_s` holds completed stretches and `last_resumed_at`
    marks the start of the current one, so the true elapsed value is always
    derivable server-side. Closing the browser, reloading, or restarting the
    container cannot lose or double-count time.
    """

    __tablename__ = "focus_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # timer | stopwatch
    label: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(64), default="building", index=True)

    planned_s: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    # running | paused | completed | cancelled

    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    accumulated_s: Mapped[float] = mapped_column(Float, default=0.0)
    last_resumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    notes: Mapped[str] = mapped_column(Text, default="")
