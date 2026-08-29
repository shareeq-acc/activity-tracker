from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, TypeDecorator
from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class UTCDateTime(TypeDecorator):
    """Timezone-correct datetimes on SQLite.

    SQLite has no native timestamp type and drops tzinfo on the way in, which
    would hand back naive datetimes that compare wrongly against aware ones.
    Everything is normalised to UTC on write and re-tagged as UTC on read, so
    the rest of the application only ever sees aware datetimes.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass
