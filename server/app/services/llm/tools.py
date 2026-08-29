"""Tools the assistant can call to read the activity database.

The assistant never sees the raw tables and never does arithmetic itself. It
calls these, which delegate to the same `analytics` / `insights` code that
renders the dashboard, so the chat answer and the charts can never disagree.

Tool schemas are declared once in a neutral form and translated per provider,
because Gemini and Ollama want different dialects of the same thing.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.focus import FocusSession
from app.services import analytics as an
from app.services.insights import build_report, fmt_hm

PERIODS = [
    "today",
    "yesterday",
    "week",
    "last_week",
    "month",
    "7d",
    "14d",
    "30d",
    "90d",
    "all",
]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_activity_summary",
        "description": (
            "Time spent per category and per app for a period. Use this for "
            "questions like 'what did I do today', 'how much did I code this "
            "week', 'what is my biggest distraction'."
        ),
        "params": {
            "period": {
                "type": "string",
                "enum": PERIODS,
                "description": "Which time window to summarise.",
            }
        },
        "required": ["period"],
    },
    {
        "name": "get_insights_report",
        "description": (
            "Full analysis for a period: growth-vs-distraction ratio, peak "
            "focus hours, deep-work streaks, focus/fragmentation score, "
            "week-over-week change, and progress against weekly goals. Use "
            "this for 'how am I doing', 'am I improving', 'when do I focus "
            "best', 'why was I unproductive'."
        ),
        "params": {
            "period": {"type": "string", "enum": PERIODS, "description": "Time window."}
        },
        "required": ["period"],
    },
    {
        "name": "get_daily_trend",
        "description": (
            "Day-by-day totals of growth, distraction, neutral and idle time. "
            "Use for trends over time, comparing days, or finding the best and "
            "worst days."
        ),
        "params": {
            "days": {
                "type": "integer",
                "description": "How many days back to include (1-180).",
            }
        },
        "required": ["days"],
    },
    {
        "name": "search_activity",
        "description": (
            "Find how much time went to a specific app, site, project or topic "
            "by matching window titles. Use for 'how long on YouTube', 'time "
            "spent on the va-platform repo', 'did I read any docs'."
        ),
        "params": {
            "query": {
                "type": "string",
                "description": "Substring to match against window titles and app names.",
            },
            "period": {"type": "string", "enum": PERIODS, "description": "Time window."},
        },
        "required": ["query", "period"],
    },
    {
        "name": "get_timer_history",
        "description": (
            "Manually started timer and stopwatch sessions, with their labels "
            "and durations. This is deliberate tracked work, separate from "
            "automatic window tracking."
        ),
        "params": {
            "period": {"type": "string", "enum": PERIODS, "description": "Time window."}
        },
        "required": ["period"],
    },
]


# --- provider translation --------------------------------------------------


def to_openai_schema() -> list[dict]:
    """Ollama (and anything OpenAI-compatible) wants JSON Schema."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": {
                    "type": "object",
                    "properties": t["params"],
                    "required": t["required"],
                },
            },
        }
        for t in TOOLS
    ]


def to_gemini_schema() -> list[dict]:
    """Gemini wants an OpenAPI subset with upper-case type names."""

    def conv(p: dict) -> dict:
        out = {"type": p["type"].upper(), "description": p.get("description", "")}
        if "enum" in p:
            out["enum"] = p["enum"]
        return out

    return [
        {
            "functionDeclarations": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {k: conv(v) for k, v in t["params"].items()},
                        "required": t["required"],
                    },
                }
                for t in TOOLS
            ]
        }
    ]


# --- execution -------------------------------------------------------------


def _hm(seconds: float) -> str:
    return fmt_hm(seconds)


def _norm_period(value: Any) -> str:
    p = str(value or "today").lower().strip()
    aliases = {
        "this_week": "week",
        "this week": "week",
        "last week": "last_week",
        "this_month": "month",
        "this month": "month",
        "last_7_days": "7d",
        "week_to_date": "week",
    }
    p = aliases.get(p, p)
    return p if p in PERIODS else "today"


async def execute(session: AsyncSession, name: str, args: dict) -> dict:
    """Run one tool call. Always returns a JSON-serialisable dict."""
    try:
        if name == "get_activity_summary":
            period = _norm_period(args.get("period"))
            start, end, label = an.resolve_period(period)
            slices = await an.load_slices(session, start, end)
            active = [s for s in slices if not s.is_idle]
            r = an.ratio(an.totals_by_bucket(slices))
            return {
                "period": label,
                "total_active": _hm(r["engaged_seconds"]),
                "growth_time": _hm(r["growth_seconds"]),
                "distraction_time": _hm(r["distraction_seconds"]),
                "idle_time": _hm(r["idle_seconds"]),
                "growth_percent": r["growth_pct"],
                "categories": [
                    {"category": c["label"], "time": _hm(c["seconds"]), "bucket": c["bucket"]}
                    for c in an.totals_by_category(active)
                ],
                "top_apps": [
                    {"app": a["app"], "time": _hm(a["seconds"]), "category": a["category"]}
                    for a in an.top_apps(active, limit=10)
                ],
            }

        if name == "get_insights_report":
            period = _norm_period(args.get("period"))
            rep = await build_report(session, period)
            wow = rep["week_over_week"]
            return {
                "period": rep["period_label"],
                "highlights": rep["highlights"],
                "growth_time": _hm(rep["ratio"]["growth_seconds"]),
                "distraction_time": _hm(rep["ratio"]["distraction_seconds"]),
                "growth_to_distraction_ratio": rep["ratio"]["ratio"],
                "focus_score_out_of_100": rep["fragmentation"]["score"],
                "app_switches_per_hour": rep["fragmentation"]["switches_per_hour"],
                "avg_uninterrupted_minutes": rep["fragmentation"]["avg_focus_minutes"],
                "longest_deep_work": (
                    _hm(rep["streaks"][0]["seconds"]) if rep["streaks"] else "none over 15m"
                ),
                "deep_work_stretches": len(rep["streaks"]),
                "peak_hours": [
                    f"{h['hour']:02d}:00 ({_hm(h['growth'])} avg growth)"
                    for h in sorted(rep["hourly"], key=lambda x: -x["total_growth"])[:4]
                    if h["total_growth"] > 0
                ],
                "week_over_week": {
                    "days_elapsed_this_week": wow["days_elapsed"],
                    "growth_this_week": _hm(wow["current"]["growth_seconds"]),
                    "growth_same_point_last_week": _hm(wow["previous"]["growth_seconds"]),
                    "biggest_changes": [
                        {
                            "category": c["label"],
                            "change": f"{'+' if c['delta'] >= 0 else '-'}{_hm(abs(c['delta']))}",
                        }
                        for c in wow["categories"][:5]
                    ],
                },
                "goals": [
                    {
                        "goal": g["label"],
                        "target_hours_per_week": g["target_hours"],
                        "done": _hm(g["actual_seconds"]),
                        "percent": g["pct"],
                        "on_pace": g["on_pace"],
                    }
                    for g in rep["goals"]
                ],
            }

        if name == "get_daily_trend":
            days = max(1, min(180, int(args.get("days") or 7)))
            start, _ = an.day_bounds(an.today_local() - timedelta(days=days - 1))
            _, end = an.day_bounds(an.today_local())
            slices = await an.load_slices(session, start, end)
            return {
                "days": [
                    {
                        "date": d["date"],
                        "growth": _hm(d["growth"]),
                        "distraction": _hm(d["distraction"]),
                        "neutral": _hm(d["neutral"]),
                    }
                    for d in an.daily_series(slices)
                ]
            }

        if name == "search_activity":
            query = str(args.get("query") or "").lower().strip()
            period = _norm_period(args.get("period"))
            if not query:
                return {"error": "query is required"}
            start, end, label = an.resolve_period(period)
            slices = await an.load_slices(session, start, end, include_idle=False)
            hits = [
                s
                for s in slices
                if query in (s.title or "").lower() or query in (s.exe or "").lower()
            ]
            total = sum(s.seconds for s in hits)
            return {
                "query": query,
                "period": label,
                "total_time": _hm(total),
                "occurrences": len(hits),
                "categories": [
                    {"category": c["label"], "time": _hm(c["seconds"])}
                    for c in an.totals_by_category(hits)
                ],
                "examples": [
                    {"app": t["app"], "title": t["title"], "time": _hm(t["seconds"])}
                    for t in an.top_titles(hits, limit=8)
                ],
            }

        if name == "get_timer_history":
            from app.api.focus import elapsed_seconds

            period = _norm_period(args.get("period"))
            start, end, label = an.resolve_period(period)
            rows = (
                (
                    await session.execute(
                        select(FocusSession).where(
                            FocusSession.started_at >= start,
                            FocusSession.started_at < end,
                        )
                    )
                )
                .scalars()
                .all()
            )
            usable = [r for r in rows if r.status != "cancelled"]
            return {
                "period": label,
                "session_count": len(usable),
                "total_time": _hm(sum(elapsed_seconds(r) for r in usable)),
                "sessions": [
                    {
                        "label": r.label or "(unlabelled)",
                        "kind": r.kind,
                        "category": r.category,
                        "time": _hm(elapsed_seconds(r)),
                        "started": an.to_local(r.started_at).strftime("%Y-%m-%d %H:%M"),
                        "status": r.status,
                    }
                    for r in sorted(usable, key=lambda r: r.started_at, reverse=True)[:25]
                ],
            }

        return {"error": f"Unknown tool: {name}"}

    except Exception as exc:  # noqa: BLE001 - surface the failure to the model
        return {"error": f"{type(exc).__name__}: {exc}"}
