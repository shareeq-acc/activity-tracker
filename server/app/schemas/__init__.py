from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# --- ingest ----------------------------------------------------------------


class SegmentIn(BaseModel):
    uid: str = Field(min_length=8, max_length=32)
    exe: str = Field(default="__unknown__", max_length=255)
    title: str = Field(default="", max_length=400)
    started_at: datetime
    ended_at: datetime
    duration_s: float = 0.0
    is_closed: bool = True


class IngestIn(BaseModel):
    host: str = Field(default="", max_length=120)
    segments: list[SegmentIn] = Field(default_factory=list, max_length=1000)


class IngestOut(BaseModel):
    accepted: int
    skipped: int


# --- focus sessions --------------------------------------------------------


class FocusStartIn(BaseModel):
    kind: str = Field(default="stopwatch", pattern="^(timer|stopwatch)$")
    label: str = Field(default="", max_length=255)
    category: str = Field(default="building", max_length=64)
    planned_s: int | None = Field(default=None, ge=1, le=24 * 3600)


class FocusPatchIn(BaseModel):
    label: str | None = Field(default=None, max_length=255)
    category: str | None = Field(default=None, max_length=64)
    notes: str | None = None


# --- goals -----------------------------------------------------------------


class GoalIn(BaseModel):
    scope: str = Field(default="category", pattern="^(category|bucket)$")
    target_key: str = Field(max_length=64)
    weekly_target_hours: float = Field(ge=0, le=168)


# --- chat ------------------------------------------------------------------


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    provider: str | None = Field(default=None, pattern="^(gemini|ollama)$")
    reset: bool = False
