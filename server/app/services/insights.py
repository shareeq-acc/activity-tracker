"""Composes the raw aggregations in `analytics` into the insights report.

Everything here is deterministic. The LLM reads this report; it does not
compute any of it.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meta import Goal
from app.services import analytics as an
from app.services.categorizer import ruleset


def fmt_hm(seconds: float) -> str:
    seconds = int(seconds)
    h, m = seconds // 3600, (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


async def week_over_week(session: AsyncSession) -> dict:
    today = an.today_local()
    this_start, _ = an.day_bounds(an.week_start(today))
    _, this_end = an.day_bounds(today)

    last_ws = an.week_start(today) - timedelta(days=7)
    last_start, _ = an.day_bounds(last_ws)
    # Compare like for like: only the same number of days into the week.
    _, last_end = an.day_bounds(last_ws + timedelta(days=today.weekday()))

    cur = an.load_slices(session, this_start, this_end)
    prev = an.load_slices(session, last_start, last_end)
    cur_slices, prev_slices = await cur, await prev

    cur_b = an.totals_by_bucket(cur_slices)
    prev_b = an.totals_by_bucket(prev_slices)

    cur_cat = {c["category"]: c["seconds"] for c in an.totals_by_category(cur_slices)}
    prev_cat = {c["category"]: c["seconds"] for c in an.totals_by_category(prev_slices)}

    rs = ruleset.get()
    deltas = []
    for key in sorted(set(cur_cat) | set(prev_cat)):
        if key == "idle":
            continue
        cat = rs.categories.get(key)
        now_s, was_s = cur_cat.get(key, 0), prev_cat.get(key, 0)
        deltas.append(
            {
                "category": key,
                "label": cat.label if cat else key.title(),
                "color": cat.color if cat else "#64748b",
                "current": now_s,
                "previous": was_s,
                "delta": now_s - was_s,
            }
        )
    deltas.sort(key=lambda d: -abs(d["delta"]))

    return {
        "days_elapsed": today.weekday() + 1,
        "current": an.ratio(cur_b),
        "previous": an.ratio(prev_b),
        "categories": deltas,
    }


async def goals_progress(session: AsyncSession) -> list[dict]:
    goals = (
        (await session.execute(select(Goal).where(Goal.active.is_(True)))).scalars().all()
    )
    if not goals:
        return []

    today = an.today_local()
    start, _ = an.day_bounds(an.week_start(today))
    _, end = an.day_bounds(today)
    slices = await an.load_slices(session, start, end)

    by_cat = {c["category"]: c["seconds"] for c in an.totals_by_category(slices)}
    by_bucket = an.totals_by_bucket(slices)
    rs = ruleset.get()

    out = []
    for g in goals:
        actual = (by_bucket if g.scope == "bucket" else by_cat).get(g.target_key, 0)
        target = g.weekly_target_hours * 3600
        cat = rs.categories.get(g.target_key)
        # Pace: how far through the week we are, Monday 00:00 to now.
        elapsed_frac = min(1.0, (today.weekday() + 1) / 7)
        out.append(
            {
                "id": g.id,
                "scope": g.scope,
                "target_key": g.target_key,
                "label": cat.label if cat else g.target_key.title(),
                "color": cat.color if cat else "#64748b",
                "target_hours": g.weekly_target_hours,
                "actual_seconds": round(actual),
                "pct": round(100 * actual / target, 1) if target else 0.0,
                "on_pace": actual >= target * elapsed_frac if target else True,
            }
        )
    return out


def _highlights(report: dict) -> list[str]:
    out: list[str] = []
    r = report["ratio"]
    label = report["period_label"].lower()

    if r["engaged_seconds"] == 0:
        return ["No activity recorded for this period yet."]

    if r["ratio"] is not None:
        out.append(
            f"Growth-to-distraction is {r['ratio']}:1 {label} "
            f"({fmt_hm(r['growth_seconds'])} building or learning vs "
            f"{fmt_hm(r['distraction_seconds'])} distracted)."
        )
    elif r["growth_seconds"]:
        out.append(
            f"{fmt_hm(r['growth_seconds'])} of growth time {label} with no distraction logged."
        )

    peak = [h for h in report["hourly"] if h["total_growth"] > 0]
    if peak:
        best = max(peak, key=lambda h: h["total_growth"])
        out.append(
            f"Peak focus hour is {best['hour']:02d}:00-{(best['hour'] + 1) % 24:02d}:00, "
            f"averaging {fmt_hm(best['growth'])} of growth work."
        )

    streaks = report["streaks"]
    if streaks:
        out.append(
            f"Longest unbroken deep-work stretch was {fmt_hm(streaks[0]['seconds'])}; "
            f"{len(streaks)} stretch(es) over 15 minutes."
        )
    else:
        out.append("No deep-work stretch reached 15 minutes uninterrupted.")

    frag = report["fragmentation"]
    if frag["active_seconds"]:
        out.append(
            f"Focus score {frag['score']}/100 - {frag['switches_per_hour']} app switches per hour, "
            f"averaging {frag['avg_focus_minutes']} minutes per stretch."
        )

    distractors = [c for c in report["categories"] if c["bucket"] == "distraction"]
    if distractors:
        top = distractors[0]
        out.append(f"Biggest drain is {top['label']} at {fmt_hm(top['seconds'])}.")

    unc = next((c for c in report["categories"] if c["category"] == "uncategorized"), None)
    if unc and unc["seconds"] > 900:
        out.append(
            f"{fmt_hm(unc['seconds'])} is uncategorized - add rules for those apps "
            f"to sharpen these numbers."
        )

    return out


async def build_report(session: AsyncSession, period: str = "today") -> dict:
    start, end, label = an.resolve_period(period)
    slices = await an.load_slices(session, start, end)
    active = [s for s in slices if not s.is_idle]

    report = {
        "period": period,
        "period_label": label,
        "start": an.to_local(start).isoformat(),
        "end": an.to_local(end).isoformat(),
        "ratio": an.ratio(an.totals_by_bucket(slices)),
        "categories": an.totals_by_category(active),
        "top_apps": an.top_apps(active),
        "top_titles": an.top_titles(active),
        "daily": an.daily_series(slices),
        "hourly": an.hourly_profile(slices),
        "streaks": an.deep_work_streaks(active),
        "fragmentation": an.fragmentation(slices),
        "week_over_week": await week_over_week(session),
        "goals": await goals_progress(session),
    }
    report["highlights"] = _highlights(report)
    return report
