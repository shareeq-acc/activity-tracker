from __future__ import annotations

from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.models.activity import Segment
from app.models.base import utcnow
from app.schemas import IngestIn, IngestOut
from app.services.categorizer import ruleset

router = APIRouter(tags=["ingest"])


def _check_token(token: str | None) -> None:
    expected = settings.ingest_token
    if not expected or expected == "change-me":
        raise HTTPException(
            status_code=503,
            detail="Server has no INGEST_TOKEN configured; refusing to accept data.",
        )
    if token != expected:
        raise HTTPException(status_code=401, detail="Bad ingest token")


@router.post("/ingest", response_model=IngestOut)
async def ingest(
    payload: IngestIn,
    x_ingest_token: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> IngestOut:
    """Accept a batch of segments from the collector.

    Idempotent: the collector re-sends its open segment on every flush so the
    dashboard stays live, and each row is UPSERTed on `uid`.
    """
    _check_token(x_ingest_token)

    rs = ruleset.get()
    accepted = skipped = 0
    now = utcnow()

    for seg in payload.segments:
        start = seg.started_at.astimezone(timezone.utc)
        end = seg.ended_at.astimezone(timezone.utc)
        duration = (end - start).total_seconds()

        # Reject nonsense rather than letting it skew the numbers. A few
        # minutes of tolerance covers ordinary clock skew on the host. Both
        # ends are checked: a segment finishing in the future would otherwise
        # sit at the top of "most recent" queries forever.
        horizon = now + timedelta(minutes=5)
        if duration < 0 or start > horizon or end > horizon:
            skipped += 1
            continue
        if seg.is_closed and duration < settings.min_segment_seconds:
            skipped += 1
            continue

        category, bucket, rule_id = rs.categorize(seg.exe, seg.title)

        stmt = sqlite_insert(Segment).values(
            uid=seg.uid,
            host=payload.host,
            exe=seg.exe,
            title=seg.title,
            category=category,
            bucket=bucket,
            rule_id=rule_id,
            started_at=start,
            ended_at=end,
            duration_s=duration,
            is_closed=seg.is_closed,
            created_at=now,
            updated_at=now,
        )
        # An open segment grows on each flush; a closed one is final.
        stmt = stmt.on_conflict_do_update(
            index_elements=[Segment.uid],
            set_={
                "ended_at": stmt.excluded.ended_at,
                "duration_s": stmt.excluded.duration_s,
                "title": stmt.excluded.title,
                "category": stmt.excluded.category,
                "bucket": stmt.excluded.bucket,
                "rule_id": stmt.excluded.rule_id,
                "is_closed": stmt.excluded.is_closed,
                "updated_at": now,
            },
        )
        await session.execute(stmt)
        accepted += 1

    await session.commit()
    return IngestOut(accepted=accepted, skipped=skipped)
