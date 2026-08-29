"""Fill the tracker with plausible history so the Insights, Timeline and chat
views have something to work with before you have collected weeks of real data.

Everything it writes is tagged with the host label DEMO-SEED, so it can be
removed later without touching anything the real collector recorded.

    python scripts/seed-demo.py            # seed 14 days
    python scripts/seed-demo.py --days 30  # seed 30 days
    python scripts/seed-demo.py --wipe     # remove seeded data, keep real data
"""

from __future__ import annotations

import argparse
import json
import random
import urllib.error
import urllib.request
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HOST_LABEL = "DEMO-SEED"
ROOT = Path(__file__).resolve().parent.parent


def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if not path.exists():
        raise SystemExit("No .env found. Copy .env.example to .env first.")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def call(base: str, path: str, token: str, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Ingest-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Server returned {e.code}: {e.read().decode()[:300]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Cannot reach the server at {base} - is the container up? ({e})")


# (exe, title, weight) pools per kind of block
BUILD = [
    ("Code.exe", "collector.py - activity-tracker - Visual Studio Code"),
    ("Code.exe", "analytics.py - activity-tracker - Visual Studio Code"),
    ("Code.exe", "main.py - va-platform - Visual Studio Code"),
    ("Code.exe", "docker-compose.yml - va-platform - Visual Studio Code"),
    ("WindowsTerminal.exe", "pwsh - docker compose up"),
    ("WindowsTerminal.exe", "pwsh - pytest"),
    ("chrome.exe", "shareeq-acc/va-platform - GitHub"),
    ("chrome.exe", "localhost:8000 - FastAPI docs"),
    ("Docker Desktop.exe", "Docker Desktop"),
]
LEARN = [
    ("chrome.exe", "FastAPI documentation - User Guide"),
    ("chrome.exe", "SQLAlchemy 2.0 documentation"),
    ("chrome.exe", "Kubernetes networking full course - YouTube"),
    ("chrome.exe", "System design primer - GitHub"),
    ("SumatraPDF.exe", "designing-data-intensive-applications.pdf"),
    ("chrome.exe", "How does TCP work - Wikipedia"),
    ("chrome.exe", "Rust ownership explained - dev.to"),
]
DISTRACT = [
    ("chrome.exe", "Top 10 fails - YouTube"),
    ("chrome.exe", "reddit - r/programming"),
    ("chrome.exe", "instagram"),
    ("chrome.exe", "(3) Home / X"),
    ("steam.exe", "Steam"),
    ("VALORANT-Win64-Shipping.exe", "VALORANT"),
]
COMMS = [
    ("Discord.exe", "general - dev server"),
    ("WhatsApp.exe", "WhatsApp"),
    ("chrome.exe", "Inbox (12) - Gmail"),
]
UTIL = [
    ("explorer.exe", "Downloads - File Explorer"),
    ("Spotify.exe", "Spotify Premium"),
]


def build_day(day_start: datetime, rng: random.Random, weekend: bool) -> list[dict]:
    """One plausible day, emitted as contiguous segments."""
    segments: list[dict] = []

    # When the day starts, and how much of it is worked.
    start_hour = rng.uniform(10.5, 13.0) if weekend else rng.uniform(8.5, 10.5)
    end_hour = rng.uniform(20.0, 23.5) if weekend else rng.uniform(18.0, 23.0)
    cursor = day_start + timedelta(hours=start_hour)
    finish = day_start + timedelta(hours=end_hour)

    # Weekends drift toward distraction; weekdays toward building.
    weights = (
        [("distract", 4), ("build", 3), ("learn", 2), ("comms", 2), ("util", 1), ("idle", 3)]
        if weekend
        else [("build", 7), ("learn", 3), ("distract", 3), ("comms", 2), ("util", 1), ("idle", 2)]
    )
    kinds = [k for k, w in weights for _ in range(w)]

    pools = {"build": BUILD, "learn": LEARN, "distract": DISTRACT, "comms": COMMS, "util": UTIL}

    while cursor < finish:
        kind = rng.choice(kinds)

        if kind == "idle":
            minutes = rng.choice([12, 20, 35, 50, 75])
            exe, title = "__idle__", "Idle"
            end = min(finish, cursor + timedelta(minutes=minutes))
            segments.append(make(exe, title, cursor, end))
            cursor = end
            continue

        # A block of focused work is several windows in a row from one pool.
        pool = pools[kind]
        block_minutes = rng.choice([25, 40, 55, 75, 95]) if kind == "build" else rng.choice([10, 18, 30, 45])
        block_end = min(finish, cursor + timedelta(minutes=block_minutes))

        while cursor < block_end:
            exe, title = rng.choice(pool)
            chunk = rng.choice([3, 6, 9, 14, 22])
            end = min(block_end, cursor + timedelta(minutes=chunk))
            if (end - cursor).total_seconds() < 60:
                break
            segments.append(make(exe, title, cursor, end))
            cursor = end

    return segments


def make(exe: str, title: str, start: datetime, end: datetime) -> dict:
    return {
        "uid": uuid.uuid4().hex,
        "exe": exe,
        "title": title,
        "started_at": start.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
        "ended_at": end.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
        "duration_s": (end - start).total_seconds(),
        "is_closed": True,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the tracker with demo history")
    ap.add_argument("--days", type=int, default=14, help="how many days back to generate")
    ap.add_argument("--wipe", action="store_true", help="remove seeded data and exit")
    ap.add_argument("--seed", type=int, default=7, help="RNG seed, for repeatability")
    args = ap.parse_args()

    env = read_env()
    token = env.get("INGEST_TOKEN", "")
    base = f"http://localhost:{env.get('APP_PORT', '8765')}"
    tz = ZoneInfo(env.get("TZ", "UTC"))

    if args.wipe:
        r = call(base, f"/api/segments?host={HOST_LABEL}", token, method="DELETE")
        print(f"Removed {r['deleted']} seeded segment(s). Real collector data untouched.")
        return 0

    if not token or token == "change-me":
        raise SystemExit("INGEST_TOKEN is not set in .env")

    rng = random.Random(args.seed)
    today = datetime.now(tz=tz).replace(hour=0, minute=0, second=0, microsecond=0)

    all_segments: list[dict] = []
    for back in range(args.days):
        day = today - timedelta(days=back)
        # Do not invent activity that has not happened yet today.
        segs = build_day(day, rng, weekend=day.weekday() >= 5)
        now = datetime.now(tz=timezone.utc)
        segs = [s for s in segs if datetime.fromisoformat(s["ended_at"]) <= now]
        all_segments.extend(segs)

    print(f"Generated {len(all_segments)} segments across {args.days} days.")

    sent = 0
    for i in range(0, len(all_segments), 500):
        chunk = all_segments[i : i + 500]
        r = call(base, "/api/ingest", token, {"host": HOST_LABEL, "segments": chunk})
        sent += r["accepted"]
    print(f"Ingested {sent} segments as host '{HOST_LABEL}'.")
    print(f"\nOpen {base} - try the Insights and Timeline tabs.")
    print("Remove it later with:  python scripts/seed-demo.py --wipe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
