# Activity Tracker

Personal time tracking for a Windows PC. It records which application and
window actually had your attention, sorts that into categories you control,
and gives you a dashboard, an insights report, a timer/stopwatch, and a chat
assistant that answers questions about your own data.

---

## Why it is split in two

A Docker container cannot see your Windows desktop. Docker Desktop runs Linux
containers inside a VM with no access to the Win32 API that reports the
focused window, so a tracker living entirely in a container physically cannot
know whether you are in VS Code or on YouTube.

So there are two pieces:

| Piece | Runs where | What it does |
|---|---|---|
| **Collector** | Natively on Windows | Samples the foreground window and the idle timer, turns that into time segments, ships them to the server |
| **Server** | In Docker | Database, categorisation, analytics, dashboard, timer, chat assistant |

The collector is ~330 lines of pure standard library — `ctypes` for Win32 and
`urllib` for HTTP. **There is nothing to pip install on the host.**

---

## Quick start

### 1. Configure

```bash
cp .env.example .env
```

Then edit `.env` and set at minimum:

- **`TZ`** — your timezone (e.g. `Asia/Karachi`, `Europe/London`). All day
  boundaries, "peak hours", and weekly rollups depend on this.
- **`INGEST_TOKEN`** — generate one:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Start the server

```bash
docker compose up -d
```

The dashboard is at **http://localhost:8765**.

### 3. Start the collector

For a quick test, double-click `collector\run-collector.bat` (a console window
shows what it is recording).

To have it run automatically at every logon, with no console window:

```powershell
powershell -ExecutionPolicy Bypass -File .\collector\install-startup.ps1
Start-ScheduledTask -TaskName ActivityTrackerCollector
```

Remove it later with `.\collector\install-startup.ps1 -Uninstall`.

The collector reads `.env` itself, so the ingest token never appears in a
command line or in the Task Scheduler UI.

---

## Trying it out before you have history

The dashboard only ever shows what the collector sent, so a fresh install is
empty until the collector has been running for a minute. Insights and Timeline
need days of data before they say anything interesting.

To fill it with plausible history so you can exercise those views immediately:

```bash
python scripts/seed-demo.py --days 14
```

Everything it writes is tagged with the host label `DEMO-SEED`. Remove it later
without touching anything real:

```bash
python scripts/seed-demo.py --wipe
```

---

## Stopping and starting

```bash
docker compose stop     # pause; data is kept
docker compose start    # resume
docker compose down     # remove the container; data is kept
```

The database is a single SQLite file at `data/tracker.db`, bind-mounted from
the host. `docker compose down` never costs you history, and backing up is
copying that one file.

While the server is down the collector keeps recording to
`collector/spool/pending.jsonl` and backfills everything on the next successful
flush. You do not lose time when the container is stopped.

---

## The five things it tracks

**Dashboard** — what is on screen right now, today's growth-vs-distraction
split, where the time went by category and by app, and a time-of-day profile.

**Timeline** — a 24-hour strip for any day, colour-coded by category, with
every session listed. Hover a block to see the window title and duration.

**Insights** — the four analyses:

- *Learning vs distraction ratio* — growth time over distraction time, with
  the trend against last week.
- *Peak focus hours and deep-work streaks* — which hours you actually do
  focused work, and your longest unbroken runs. A run survives interruptions
  under 3 minutes, so a quick Slack check does not end a two-hour session, but
  a twenty-minute detour does.
- *Fragmentation / focus score* — app switches per hour and average minutes
  per stretch, scored out of 100. Switches are counted on the *application*,
  not the window title, so moving between files in one editor is not scored as
  distraction.
- *Week-over-week and goals* — this week against the same point last week, plus
  weekly hour targets per category with on-pace tracking.

**Timer & stopwatch** — labelled, categorised manual sessions. Elapsed time is
derived from stored anchors on the server, never from a client-side counter, so
reloading the page, closing the laptop, or restarting the container cannot lose
or double-count time. A countdown that expires while you were away is completed
and back-dated to the moment it actually hit zero.

**Ask** — chat over your own data. The assistant has no direct database access;
it calls the same analytics functions that draw the charts, so its numbers and
the dashboard can never disagree.

---

## Categorisation

`rules.yml` maps applications and window titles to categories. Rules are
evaluated top to bottom and **the first match wins**, which is why specific
browser rules sit above the generic one.

```yaml
- id: dev-web
  category: building
  exe: [chrome.exe, msedge.exe, firefox.exe]
  title_any: [github, localhost, stack overflow]
```

Match fields, all optional and case-insensitive:

| Field | Meaning |
|---|---|
| `exe` | Process name, supports `*` globs |
| `title_any` | Matches if **any** of these appear in the window title |
| `title_all` | Matches only if **all** appear |
| `title_not` | Veto — skip this rule if any appear |

Categories roll up into buckets — `growth`, `neutral`, `distraction`, `idle` —
and the buckets drive the learning-vs-distraction ratio.

After editing, click **Reload rules & re-categorise** in Settings. History is
re-categorised in place, so fixing a rule fixes the past too and your totals
stay consistent with themselves.

Settings also lists the apps that matched no rule, ranked by how much time they
cost you, so you know which ones are worth a rule.

---

## The chat assistant

Two providers, switchable from the dropdown in the **Ask** tab:

### Gemini

Set `GEMINI_API_KEY` in `.env` (get one at https://aistudio.google.com/apikey)
and restart. Fast, and better at multi-step questions.

### Local model via Ollama

Nothing leaves your machine.

**Install Ollama natively on Windows** (recommended) from https://ollama.com,
then:

```bash
ollama pull qwen2.5:7b-instruct
```

The container reaches it at `host.docker.internal:11434`, which is the default
in `.env`. `qwen2.5:7b-instruct` is the pick here because it handles tool
calling reliably at 7B; many models that size do not.

A note on your hardware: **Docker Desktop on Windows cannot pass an AMD GPU
into a container**, so the bundled Ollama container is CPU-only. A native
install can at least attempt ROCm — though the RX 6600 is `gfx1032`, which
Ollama does not officially support, and may need:

```
setx HSA_OVERRIDE_GFX_VERSION 10.3.0
```

If it falls back to CPU, a Ryzen 5 3600 runs a 7B q4 at usable but not fast
speeds; expect the first reply to take a while as the model loads. If it is too
slow, `qwen2.5:3b-instruct` is a reasonable trade.

To run Ollama in Docker instead of natively, set
`OLLAMA_BASE_URL=http://ollama:11434` in `.env` and:

```bash
docker compose --profile local-llm up -d
```

---

## How the tracking stays accurate

The collector was the part worth being careful about:

- **Exact boundaries.** Windows reports how long ago input stopped, so an idle
  segment starts at the moment you actually stopped typing, not at whichever
  5-second poll noticed. Same on the way back.
- **Watching is not idling.** A 40-minute video involves no input at all. Media
  applications and browsers get a much longer idle timeout (`MEDIA_IDLE_SECONDS`,
  default 90 minutes) than everything else (`IDLE_SECONDS`, default 5 minutes).
- **Sleep is not work.** A wall-clock jump larger than the poll interval means
  suspend, hibernate, or a stall. The open segment is closed at the last moment
  the collector can actually vouch for, rather than crediting you eight hours of
  "building" while the machine was off.
- **Nothing is lost.** Closed segments are written to an on-disk spool *before*
  any network call. Server down, laptop asleep, process killed — the data is
  already on disk and backfills later.
- **No duplicates.** Every segment carries a client-generated uid and is
  upserted server-side, so the open segment can be re-sent on every flush to
  keep the dashboard live, at zero risk.
- **Locked means locked.** A locked workstation is recorded separately from
  ordinary idle.

Storage is always UTC; reporting is always your local timezone. Segments are
split across local day and hour boundaries, so one overnight idle segment does
not dump eight hours into whichever day it happened to start in.

---

## Privacy

Everything is local: SQLite on your disk, a container on your machine. Window
titles are stored because they carry the signal (a `github.com` tab is not a
`youtube.com` tab), but nothing is sent anywhere.

The one exception is the chat assistant when set to **Gemini** — questions and
the tool results answering them go to Google's API. Use the Ollama provider if
you would rather that never happen.

To stop recording titles entirely, set `TITLE_MODE=redacted` in `.env`. You keep
per-application totals and lose per-window detail.

---

## Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `TZ` | `UTC` | Timezone for all day/week boundaries and reporting |
| `APP_PORT` | `8765` | Host port for the dashboard |
| `INGEST_TOKEN` | — | Shared secret; the server refuses data without it |
| `MIN_SEGMENT_SECONDS` | `2` | Segments shorter than this are discarded as noise |
| `IDLE_SECONDS` | `300` | No input for this long counts as idle |
| `MEDIA_IDLE_SECONDS` | `5400` | Longer idle timeout for media apps and browsers |
| `POLL_SECONDS` | `5` | How often the collector samples |
| `FLUSH_SECONDS` | `30` | How often it uploads |
| `TITLE_MODE` | `full` | `full` or `redacted` |
| `DEFAULT_LLM_PROVIDER` | `ollama` | `ollama` or `gemini` |
| `GEMINI_API_KEY` | — | Enables the Gemini provider |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Where Ollama lives |
| `OLLAMA_MODEL` | `qwen2.5:7b-instruct` | Local model to use |

---

## API

Interactive docs at http://localhost:8765/docs.

| Endpoint | Purpose |
|---|---|
| `POST /api/ingest` | Collector uploads segments (needs `X-Ingest-Token`) |
| `GET /api/live` | Current window and today's totals |
| `GET /api/summary?period=` | Category and app breakdown |
| `GET /api/timeline?day=` | One day, segment by segment |
| `GET /api/insights?period=` | Full insights report |
| `GET/POST /api/focus` | Timer and stopwatch sessions |
| `POST /api/rules/reload` | Re-read `rules.yml` and re-categorise history |
| `POST /api/chat` | Ask the assistant a question |

Periods: `today`, `yesterday`, `week`, `last_week`, `month`, `7d`, `30d`,
`90d`, `all`.

---

## Troubleshooting

**Status dot says "no data yet"** — the collector is not running, or the token
in `.env` does not match. Run `run-collector.bat` and read the console: it says
outright if the server rejects the token.

**"holding N segments in spool"** — the server is unreachable. Nothing is lost;
it backfills when the server returns.

**Everything is "Uncategorized"** — check Settings for the unmatched app list
and add rules for the ones that matter.

**Chat says Ollama is unreachable** — Ollama is not installed or not running on
the host. `ollama serve` starts it; `ollama list` shows what is pulled.
