"""Second-opinion categorisation for windows the rules cannot resolve.

Keyword rules handle the easy 90%: a process called Code.exe is building, full
stop. They fall apart on browsers, where the process name says nothing and
every YouTube video shares one domain - only the video title separates a
Kubernetes course from a VALORANT stream, and no keyword list covers that tail.

So: rules first, always. Whatever they cannot resolve confidently is sent to
the LLM once, and the answer is cached forever against the exact title. The
model is asked about distinct titles, not segments, so a video watched across
five sittings costs one classification. Manual overrides win over everything
and are never re-decided.
"""

from __future__ import annotations

import json
import re

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import Segment
from app.models.classification import TitleClassification
from app.services.categorizer import ruleset

# Titles carry volatile noise - unread counts, playback position, dirty-file
# markers. Normalising means "(3) Inbox" and "(7) Inbox" are one cache entry
# rather than two, which matters a lot when the model costs minutes per call.
_NOISE = [
    (re.compile(r"^\(\d+\)\s*"), ""),           # (3) Gmail
    (re.compile(r"^\d+\s*[-|]\s*"), ""),        # 5 - something
    (re.compile(r"^●\s*"), ""),                 # unsaved-file dot
    (re.compile(r"\s*[-–—]\s*\d+%\s*$"), ""),   # trailing percentages
    (re.compile(r"\s+"), " "),
]

MAX_TITLE_KEY = 400


def title_key(title: str) -> str:
    key = (title or "").strip().lower()
    for pattern, repl in _NOISE:
        key = pattern.sub(repl, key)
    return key.strip()[:MAX_TITLE_KEY]


class _Cache:
    """In-memory mirror of the table, so ingest stays a hot path with no I/O."""

    def __init__(self) -> None:
        self._map: dict[tuple[str, str], tuple[str, str]] = {}
        self.loaded = False

    async def load(self, session: AsyncSession) -> None:
        rows = (await session.execute(select(TitleClassification))).scalars().all()
        self._map = {
            (r.exe.lower(), r.title_key): (r.category, r.source) for r in rows
        }
        self.loaded = True

    def get(self, exe: str, title: str) -> tuple[str, str] | None:
        return self._map.get(((exe or "").lower(), title_key(title)))

    def put(self, exe: str, title: str, category: str, source: str) -> None:
        self._map[((exe or "").lower(), title_key(title))] = (category, source)

    def clear(self) -> None:
        self._map.clear()
        self.loaded = False


cache = _Cache()


def resolve(exe: str, title: str) -> tuple[str, str, str]:
    """-> (category, bucket, rule_id). The single source of truth for ingest.

    A cached decision beats the rules, because the cache only ever holds
    entries for cases the rules already admitted they could not resolve, plus
    manual overrides which outrank everything.
    """
    rs = ruleset.get()
    category, bucket, rule_id, ambiguous = rs.categorize(exe, title)

    hit = cache.get(exe, title)
    if hit is not None:
        cached_category, source = hit
        if source == "manual" or ambiguous:
            if cached_category in rs.categories:
                return cached_category, rs.bucket_of(cached_category), f"cache:{source}"

    return category, bucket, rule_id


async def pending_titles(session: AsyncSession, limit: int = 40) -> list[dict]:
    """Distinct (exe, title) pairs the rules could not resolve, worst first.

    Ranked by how much time they account for, so a slow local model spends its
    effort on the windows that actually move the numbers.
    """
    rs = ruleset.get()
    if not cache.loaded:
        await cache.load(session)

    rows = (
        await session.execute(
            select(
                Segment.exe,
                Segment.title,
                func.sum(Segment.duration_s).label("secs"),
            )
            .group_by(Segment.exe, Segment.title)
            .order_by(func.sum(Segment.duration_s).desc())
            .limit(1500)
        )
    ).all()

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for exe, title, secs in rows:
        if exe in ("__idle__", "__locked__"):
            continue
        _, _, _, ambiguous = rs.categorize(exe, title or "")
        if not ambiguous:
            continue
        if cache.get(exe, title or "") is not None:
            continue
        key = (exe.lower(), title_key(title or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append({"exe": exe, "title": title or "", "seconds": round(secs or 0)})
        if len(out) >= limit:
            break
    return out


def _prompt(items: list[dict]) -> str:
    rs = ruleset.get()
    catalogue = "\n".join(
        f"  {key}: {cat.label} ({cat.bucket})"
        for key, cat in rs.categories.items()
        if key not in ("idle", "uncategorized")
    )
    listing = "\n".join(
        f'{i + 1}. app="{it["exe"]}" title="{it["title"][:180]}"'
        for i, it in enumerate(items)
    )
    return f"""Classify each computer window into exactly one category.

CATEGORIES
{catalogue}

GUIDANCE
- Judge by what the person is actually doing, not the application. A browser
  showing a programming tutorial is learning; the same browser showing a
  gaming stream is gaming; showing a sitcom is watching.
- A YouTube video teaching a skill is learning. A YouTube video of someone
  playing a game is gaming. A YouTube video for fun is watching.
- Documentation, courses, articles and reference material are learning.
- Writing code, repositories, terminals, servers, cloud consoles, design and
  AI coding assistants are building.
- If a title is truly uninformative (for example just "New Tab"), use browsing.

WINDOWS
{listing}

Reply with ONLY a JSON array, one object per window, in the same order:
[{{"n": 1, "category": "learning", "confidence": 0.9}}]
No prose, no markdown fence."""


def _parse(raw: str, items: list[dict]) -> list[dict]:
    rs = ruleset.get()
    text = raw.strip()
    # Models wrap JSON in fences no matter how firmly you ask them not to.
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []

    out = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("n", 0)) - 1
        except (TypeError, ValueError):
            continue
        category = str(entry.get("category", "")).strip().lower()
        if not (0 <= idx < len(items)) or category not in rs.categories:
            continue
        try:
            confidence = float(entry.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        out.append(
            {
                "exe": items[idx]["exe"],
                "title": items[idx]["title"],
                "category": category,
                "confidence": max(0.0, min(1.0, confidence)),
            }
        )
    return out


async def classify(
    session: AsyncSession, provider: str, limit: int = 20, batch_size: int = 10
) -> dict:
    """Classify pending titles and cache the results.

    Batched because a 7B model on CPU costs minutes per call; ten titles per
    call is roughly the same wall time as one.
    """
    from app.services.llm import gemini, ollama

    items = await pending_titles(session, limit=limit)
    if not items:
        return {"pending": 0, "classified": 0, "results": []}

    stored: list[dict] = []
    errors: list[str] = []

    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        prompt = _prompt(batch)
        try:
            if provider == "gemini":
                res = await gemini.chat_plain(prompt)
            else:
                res = await ollama.chat_plain(prompt)
        except Exception as exc:  # noqa: BLE001 - report, do not abort the rest
            errors.append(f"{type(exc).__name__}: {exc}")
            break

        for decision in _parse(res, batch):
            await upsert(
                session,
                decision["exe"],
                decision["title"],
                decision["category"],
                source="llm",
                confidence=decision["confidence"],
            )
            stored.append(decision)

    await session.commit()
    applied = await reapply(session) if stored else 0

    return {
        "pending": len(items),
        "classified": len(stored),
        "segments_updated": applied,
        "errors": errors,
        "results": stored,
    }


async def upsert(
    session: AsyncSession,
    exe: str,
    title: str,
    category: str,
    source: str = "llm",
    confidence: float = 1.0,
    reason: str = "",
) -> None:
    key = title_key(title)
    existing = (
        await session.execute(
            select(TitleClassification).where(
                TitleClassification.exe == exe, TitleClassification.title_key == key
            )
        )
    ).scalars().first()

    if existing is None:
        session.add(
            TitleClassification(
                exe=exe,
                title_key=key,
                title=title,
                category=category,
                source=source,
                confidence=confidence,
                reason=reason,
            )
        )
    else:
        # Never let an automated pass overwrite a human decision.
        if existing.source == "manual" and source != "manual":
            return
        existing.category = category
        existing.source = source
        existing.confidence = confidence
        existing.reason = reason

    cache.put(exe, title, category, source)


async def reapply(session: AsyncSession) -> int:
    """Re-run resolution over stored history so past segments get the new answer."""
    if not cache.loaded:
        await cache.load(session)

    pairs = (await session.execute(select(Segment.exe, Segment.title).distinct())).all()
    grouped: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for exe, title in pairs:
        category, bucket, rule_id = resolve(exe, title or "")
        grouped.setdefault((category, bucket, rule_id), []).append((exe, title))

    changed = 0
    for (category, bucket, rule_id), members in grouped.items():
        for i in range(0, len(members), 400):
            chunk = members[i : i + 400]
            result = await session.execute(
                Segment.__table__.update()
                .where(
                    tuple_(Segment.exe, Segment.title).in_(chunk),
                    (Segment.category != category) | (Segment.rule_id != rule_id),
                )
                .values(category=category, bucket=bucket, rule_id=rule_id)
            )
            changed += result.rowcount or 0
    await session.commit()
    return changed
