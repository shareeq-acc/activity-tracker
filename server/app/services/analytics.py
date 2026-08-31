"""Deterministic analytics over the segment table.

Every number the UI shows and every number the chat assistant quotes comes
from this module. There is no second implementation, so the assistant cannot
drift away from what the dashboard says.

Two things are handled carefully here:

  * Segments are clipped to the query window and split across local day and
    hour boundaries. A single overnight idle segment must not dump eight hours
    into whichever day it happened to start in.

  * All storage is UTC, all reporting is in the configured local timezone.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.activity import Segment
from app.services.categorizer import ruleset

IDLE_EXES = {"__idle__", "__locked__"}

APP_NAMES = {
    "code.exe": "VS Code",
    "code - insiders.exe": "VS Code Insiders",
    "cursor.exe": "Cursor",
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "brave.exe": "Brave",
    "zen.exe": "Zen Browser",
    "windowsterminal.exe": "Windows Terminal",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell 7",
    "explorer.exe": "File Explorer",
    "discord.exe": "Discord",
    "slack.exe": "Slack",
    "whatsapp.exe": "WhatsApp",
    "spotify.exe": "Spotify",
    "steam.exe": "Steam",
    "obsidian.exe": "Obsidian",
    "notion.exe": "Notion",
    "docker desktop.exe": "Docker Desktop",
    "idea64.exe": "IntelliJ IDEA",
    "pycharm64.exe": "PyCharm",
    "sumatrapdf.exe": "SumatraPDF",
    "claude.exe": "Claude",
    "__idle__": "Idle",
    "__locked__": "Locked",
    "__unknown__": "Unknown app",
}


def app_name(exe: str) -> str:
    key = (exe or "").lower()
    if key in APP_NAMES:
        return APP_NAMES[key]
    return (exe or "Unknown").removesuffix(".exe").removesuffix(".EXE")


def local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(settings.tz)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(local_tz())


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC bounds of one *local* calendar day."""
    tz = local_tz()
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def today_local() -> date:
    return datetime.now(tz=local_tz()).date()


def week_start(day: date) -> date:
    """Monday of the week containing `day`."""
    return day - timedelta(days=day.weekday())


def resolve_period(period: str) -> tuple[datetime, datetime, str]:
    """Turn a friendly period name into a UTC window plus a display label."""
    today = today_local()
    period = (period or "today").lower().strip()

    if period == "today":
        s, e = day_bounds(today)
        return s, e, "Today"
    if period == "yesterday":
        s, e = day_bounds(today - timedelta(days=1))
        return s, e, "Yesterday"
    if period in ("week", "this_week"):
        s, _ = day_bounds(week_start(today))
        _, e = day_bounds(today)
        return s, e, "This week"
    if period == "last_week":
        ws = week_start(today) - timedelta(days=7)
        s, _ = day_bounds(ws)
        _, e = day_bounds(ws + timedelta(days=6))
        return s, e, "Last week"
    if period == "month":
        s, _ = day_bounds(today.replace(day=1))
        _, e = day_bounds(today)
        return s, e, "This month"
    if period.endswith("d") and period[:-1].isdigit():
        n = int(period[:-1])
        s, _ = day_bounds(today - timedelta(days=n - 1))
        _, e = day_bounds(today)
        return s, e, f"Last {n} days"
    if period == "all":
        return datetime(2000, 1, 1, tzinfo=timezone.utc), datetime.now(tz=timezone.utc), "All time"

    s, e = day_bounds(today)
    return s, e, "Today"


# ---------------------------------------------------------------------------
# Loading + clipping
# ---------------------------------------------------------------------------


@dataclass
class Slice:
    """A segment clipped to the query window."""

    exe: str
    title: str
    category: str
    bucket: str
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())

    @property
    def is_idle(self) -> bool:
        return self.exe in IDLE_EXES


async def load_slices(
    session: AsyncSession,
    start: datetime,
    end: datetime,
    include_idle: bool = True,
) -> list[Slice]:
    stmt = (
        select(Segment)
        .where(Segment.ended_at > start, Segment.started_at < end)
        .order_by(Segment.started_at)
    )
    rows = (await session.execute(stmt)).scalars().all()

    out: list[Slice] = []
    for r in rows:
        if not include_idle and r.exe in IDLE_EXES:
            continue
        s = max(r.started_at, start)
        e = min(r.ended_at, end)
        if e <= s:
            continue
        out.append(Slice(r.exe, r.title, r.category, r.bucket, s, e))
    return out


def split_by(slices: list[Slice], unit: str) -> list[tuple[datetime, Slice]]:
    """Split slices on local day or hour boundaries.

    Returns (local_bucket_start, sub_slice) pairs so long segments are
    attributed to every period they actually span.
    """
    tz = local_tz()
    step = timedelta(days=1) if unit == "day" else timedelta(hours=1)
    out: list[tuple[datetime, Slice]] = []

    for sl in slices:
        cursor = sl.start
        while cursor < sl.end:
            local = cursor.astimezone(tz)
            if unit == "day":
                bucket_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                bucket_local = local.replace(minute=0, second=0, microsecond=0)
            bucket_end = (bucket_local + step).astimezone(timezone.utc)
            piece_end = min(sl.end, bucket_end)
            out.append(
                (
                    bucket_local,
                    Slice(sl.exe, sl.title, sl.category, sl.bucket, cursor, piece_end),
                )
            )
            cursor = piece_end
    return out


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


def totals_by_category(slices: list[Slice]) -> list[dict]:
    rs = ruleset.get()
    acc: dict[str, float] = defaultdict(float)
    for sl in slices:
        acc[sl.category] += sl.seconds
    out = []
    for key, secs in acc.items():
        cat = rs.categories.get(key)
        out.append(
            {
                "category": key,
                "label": cat.label if cat else key.title(),
                "color": cat.color if cat else "#64748b",
                "bucket": cat.bucket if cat else "neutral",
                "seconds": round(secs),
            }
        )
    return sorted(out, key=lambda d: -d["seconds"])


def totals_by_bucket(slices: list[Slice]) -> dict[str, float]:
    acc: dict[str, float] = defaultdict(float)
    for sl in slices:
        acc[sl.bucket] += sl.seconds
    return {k: round(v) for k, v in acc.items()}


def top_apps(slices: list[Slice], limit: int = 12) -> list[dict]:
    rs = ruleset.get()
    acc: dict[str, float] = defaultdict(float)
    # An app like a browser spans many categories. Labelling it with whichever
    # category happened to appear first is arbitrary and actively misleading -
    # it once reported Chrome as "Social media" off a single early tab. Use the
    # category the app actually spent the most time in, and say how dominant
    # that is so a split app can be recognised as split.
    per_cat: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for sl in slices:
        acc[sl.exe] += sl.seconds
        per_cat[sl.exe][sl.category] += sl.seconds

    rows = sorted(acc.items(), key=lambda kv: -kv[1])[:limit]
    out = []
    for exe, secs in rows:
        breakdown = per_cat[exe]
        top_cat, top_secs = max(breakdown.items(), key=lambda kv: kv[1])
        cat = rs.categories.get(top_cat)
        out.append(
            {
                "exe": exe,
                "app": app_name(exe),
                "category": top_cat,
                "category_label": cat.label if cat else top_cat.title(),
                "category_share": round(100 * top_secs / secs, 1) if secs else 0.0,
                "color": cat.color if cat else "#64748b",
                "seconds": round(secs),
            }
        )
    return out


def top_titles(slices: list[Slice], limit: int = 15) -> list[dict]:
    acc: dict[tuple[str, str], float] = defaultdict(float)
    for sl in slices:
        if sl.is_idle or not sl.title:
            continue
        acc[(sl.exe, sl.title)] += sl.seconds
    rows = sorted(acc.items(), key=lambda kv: -kv[1])[:limit]
    return [
        {"app": app_name(exe), "title": title, "seconds": round(secs)}
        for (exe, title), secs in rows
    ]


def daily_series(slices: list[Slice]) -> list[dict]:
    acc: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for bucket_local, piece in split_by(slices, "day"):
        acc[bucket_local.date().isoformat()][piece.bucket] += piece.seconds
    return [
        {
            "date": day,
            "growth": round(vals.get("growth", 0)),
            "neutral": round(vals.get("neutral", 0)),
            "distraction": round(vals.get("distraction", 0)),
            "idle": round(vals.get("idle", 0)),
        }
        for day, vals in sorted(acc.items())
    ]


def hourly_profile(slices: list[Slice]) -> list[dict]:
    """Average seconds per hour-of-day, over the days actually observed."""
    acc: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    days: set[date] = set()
    for bucket_local, piece in split_by(slices, "hour"):
        acc[bucket_local.hour][piece.bucket] += piece.seconds
        days.add(bucket_local.date())
    n = max(1, len(days))
    return [
        {
            "hour": h,
            "growth": round(acc[h].get("growth", 0) / n),
            "neutral": round(acc[h].get("neutral", 0) / n),
            "distraction": round(acc[h].get("distraction", 0) / n),
            "total_growth": round(acc[h].get("growth", 0)),
        }
        for h in range(24)
    ]


def deep_work_streaks(
    slices: list[Slice], tolerance_s: float = 180, min_streak_s: float = 900
) -> list[dict]:
    """Merged runs of growth-bucket time, tolerating brief interruptions.

    A 90-second detour to Slack does not end a two-hour build session, but a
    twenty-minute one does.
    """
    growth = sorted((sl for sl in slices if sl.bucket == "growth"), key=lambda s: s.start)
    if not growth:
        return []

    runs: list[list[datetime]] = []
    cur_start, cur_end = growth[0].start, growth[0].end
    for sl in growth[1:]:
        if (sl.start - cur_end).total_seconds() <= tolerance_s:
            cur_end = max(cur_end, sl.end)
        else:
            runs.append([cur_start, cur_end])
            cur_start, cur_end = sl.start, sl.end
    runs.append([cur_start, cur_end])

    out = [
        {
            "start": to_local(s).isoformat(),
            "end": to_local(e).isoformat(),
            "seconds": round((e - s).total_seconds()),
        }
        for s, e in runs
        if (e - s).total_seconds() >= min_streak_s
    ]
    return sorted(out, key=lambda d: -d["seconds"])


def fragmentation(slices: list[Slice]) -> dict:
    """How much the day is chopped up.

    Switches are counted on the application, not the window title, so moving
    between files in one editor is not scored as distraction.
    """
    active = [sl for sl in slices if not sl.is_idle]
    active.sort(key=lambda s: s.start)
    if not active:
        return {
            "active_seconds": 0,
            "switches": 0,
            "switches_per_hour": 0.0,
            "avg_focus_minutes": 0.0,
            "score": 0,
        }

    total = sum(sl.seconds for sl in active)
    switches = 0
    prev = None
    for sl in active:
        if prev is not None and sl.exe != prev:
            switches += 1
        prev = sl.exe

    hours = total / 3600 or 1e-9
    per_hour = switches / hours
    avg_focus = (total / max(1, switches + 1)) / 60

    # 0-100, where 100 is a completely unbroken stretch. ~30 switches/hour is
    # treated as maximally fragmented.
    score = max(0, min(100, round(100 - (per_hour / 30) * 100)))

    return {
        "active_seconds": round(total),
        "switches": switches,
        "switches_per_hour": round(per_hour, 1),
        "avg_focus_minutes": round(avg_focus, 1),
        "score": score,
    }


def ratio(buckets: dict[str, float]) -> dict:
    growth = buckets.get("growth", 0)
    distraction = buckets.get("distraction", 0)
    neutral = buckets.get("neutral", 0)
    engaged = growth + distraction + neutral
    return {
        "growth_seconds": round(growth),
        "distraction_seconds": round(distraction),
        "neutral_seconds": round(neutral),
        "idle_seconds": round(buckets.get("idle", 0)),
        "engaged_seconds": round(engaged),
        "growth_pct": round(100 * growth / engaged, 1) if engaged else 0.0,
        "distraction_pct": round(100 * distraction / engaged, 1) if engaged else 0.0,
        # growth : distraction, the headline number
        "ratio": round(growth / distraction, 2) if distraction else None,
    }
