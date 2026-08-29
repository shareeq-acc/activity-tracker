"""
Host-side activity collector for Windows.

Watches the foreground window and the system idle timer, turns that into
discrete time segments, and ships them to the tracker server.

Design notes, because the accuracy of everything downstream depends on this:

  * Zero third-party dependencies. ctypes for Win32, urllib for HTTP. Nothing
    to install, nothing to keep up to date, nothing that can break on a Python
    upgrade.

  * Segments are closed on the *exact* boundary, not on the poll tick. When
    input stops, Windows tells us how long ago it stopped, so an idle segment
    starts at (now - idle_seconds) rather than at whichever 5-second tick
    happened to notice. Same on the way back.

  * Every segment carries a client-generated uid and is UPSERTed server-side.
    The open segment is re-sent every flush so the dashboard is live; re-sends
    are therefore free and duplicates are impossible.

  * Closed segments are appended to an on-disk spool before any network call.
    If the server is down, the laptop sleeps, or the process is killed, the
    data is already on disk and gets backfilled on the next successful flush.

  * Suspend/hibernate is detected by comparing wall-clock movement against the
    poll interval. A gap closes the open segment at the last tick we can
    actually vouch for, rather than silently crediting you eight hours of
    "building" while the machine was off.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Win32 plumbing
# ---------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.CloseDesktop.argtypes = [wintypes.HANDLE]

kernel32.GetTickCount.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

IDLE_EXE = "__idle__"
LOCKED_EXE = "__locked__"
UNKNOWN_EXE = "__unknown__"


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input, system-wide."""
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    # GetTickCount is a 32-bit ms counter that wraps roughly every 49.7 days.
    delta = (kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
    return delta / 1000.0


def is_locked() -> bool:
    """True when the workstation is locked or a secure desktop is up."""
    handle = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not handle:
        return True
    user32.CloseDesktop(handle)
    return False


def foreground_window() -> tuple[str, str]:
    """(exe_name, window_title) of the focused window."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return UNKNOWN_EXE, ""

    length = user32.GetWindowTextLengthW(hwnd)
    title = ""
    if length:
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return UNKNOWN_EXE, title

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        # Elevated or protected process; the title is still useful.
        return UNKNOWN_EXE, title
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name, title
        return UNKNOWN_EXE, title
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_env_file(path: Path) -> int:
    """Read the project's .env so the collector needs no arguments.

    Keeps the ingest token out of command lines and out of the Task Scheduler
    UI. Real environment variables always win, so you can still override.
    """
    if not path.exists():
        return 0
    loaded = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        # TZ is a server-side display setting holding an IANA name. The Windows
        # C runtime does not understand those and silently falls back to UTC,
        # which would skew this process's log timestamps. Segment times are
        # epoch-based and unaffected either way, but the logs should still read
        # correctly, so never import TZ here.
        if key == "TZ":
            continue
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _env(name: str, default):
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw


class Config:
    def __init__(self, args: argparse.Namespace) -> None:
        # Falls back to the port the compose stack publishes, so .env alone is
        # enough configuration.
        default_server = f"http://localhost:{_env('APP_PORT', '8765')}"
        self.server = str(args.server or _env("TRACKER_SERVER", default_server)).rstrip("/")
        self.token = str(args.token or _env("INGEST_TOKEN", ""))
        self.poll = float(args.poll or _env("POLL_SECONDS", 5))
        self.flush = float(args.flush or _env("FLUSH_SECONDS", 30))

        # Watching a 40-minute video involves no input at all, so a short idle
        # timeout would silently reclassify it as "away". Media apps get a much
        # longer leash.
        self.idle_threshold = float(_env("IDLE_SECONDS", 300))
        self.media_idle_threshold = float(_env("MEDIA_IDLE_SECONDS", 5400))
        self.media_exe = {
            e.strip().lower()
            for e in str(
                _env(
                    "MEDIA_EXE",
                    "vlc.exe,mpv.exe,mpc-hc64.exe,potplayermini64.exe,netflix.exe,"
                    "video.ui.exe,chrome.exe,msedge.exe,firefox.exe,brave.exe,zen.exe",
                )
            ).split(",")
            if e.strip()
        }

        self.min_segment = float(_env("MIN_SEGMENT_SECONDS", 2))
        self.host = str(_env("TRACKER_HOST_LABEL", socket.gethostname()))
        self.spool_path = Path(args.spool or _env("SPOOL_PATH", Path(__file__).parent / "spool" / "pending.jsonl"))
        self.spool_max_bytes = int(_env("SPOOL_MAX_BYTES", 64 * 1024 * 1024))
        self.verbose = bool(args.verbose)

        # Redact window titles if you would rather not store them at all.
        self.title_mode = str(_env("TITLE_MODE", "full")).lower()


def log(cfg: Config, msg: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def vlog(cfg: Config, msg: str) -> None:
    if cfg.verbose:
        log(cfg, msg)


# ---------------------------------------------------------------------------
# Segment tracking
# ---------------------------------------------------------------------------


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


class Segment:
    __slots__ = ("uid", "exe", "title", "started_at", "ended_at")

    def __init__(self, exe: str, title: str, started_at: float) -> None:
        self.uid = uuid.uuid4().hex
        self.exe = exe
        self.title = title
        self.started_at = started_at
        self.ended_at = started_at

    @property
    def key(self) -> tuple[str, str]:
        return (self.exe, self.title)

    def payload(self, closed: bool) -> dict:
        return {
            "uid": self.uid,
            "exe": self.exe,
            "title": self.title,
            "started_at": iso(self.started_at),
            "ended_at": iso(self.ended_at),
            "duration_s": round(max(0.0, self.ended_at - self.started_at), 3),
            "is_closed": closed,
        }


class Collector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.current: Segment | None = None
        self.pending: list[dict] = []
        self.last_tick = time.time()
        self.last_flush = 0.0
        self.running = True
        self.cfg.spool_path.parent.mkdir(parents=True, exist_ok=True)

    # -- title handling ----------------------------------------------------

    def clean_title(self, title: str) -> str:
        if self.cfg.title_mode == "redacted":
            return ""
        title = " ".join(title.split())
        return title[:400]

    # -- segment lifecycle -------------------------------------------------

    def close_current(self, at: float) -> None:
        seg = self.current
        self.current = None
        if seg is None:
            return
        seg.ended_at = max(seg.started_at, at)
        if (seg.ended_at - seg.started_at) < self.cfg.min_segment:
            vlog(self.cfg, f"drop short segment {seg.exe!r}")
            return
        self.pending.append(seg.payload(closed=True))
        vlog(
            self.cfg,
            f"closed {seg.exe} ({seg.ended_at - seg.started_at:.0f}s) :: {seg.title[:60]}",
        )

    def observe(self, exe: str, title: str, boundary: float, now: float) -> None:
        title = self.clean_title(title)
        if self.current is not None and self.current.key == (exe, title):
            self.current.ended_at = now
            return
        self.close_current(boundary)
        self.current = Segment(exe, title, boundary)
        self.current.ended_at = now
        vlog(self.cfg, f"start  {exe} :: {title[:60]}")

    def tick(self) -> None:
        now = time.time()
        cfg = self.cfg

        # Suspend / hibernate / long stall: we cannot vouch for the gap, so we
        # end the open segment where our knowledge ends.
        prev_tick = self.last_tick
        gap_limit = max(cfg.poll * 3, cfg.poll + 20)
        if now - prev_tick > gap_limit:
            log(cfg, f"time gap of {now - prev_tick:.0f}s (sleep or stall) - closing segment")
            self.close_current(prev_tick)
        self.last_tick = now

        idle = idle_seconds()

        if is_locked():
            # We do not know exactly when the lock happened. The last input is
            # the best lower bound, clamped to the previous tick so a stale
            # idle timer cannot back-date the segment past what we observed.
            self.observe(LOCKED_EXE, "Workstation locked", now - min(idle, now - prev_tick), now)
            return

        exe, title = foreground_window()
        threshold = (
            cfg.media_idle_threshold if exe.lower() in cfg.media_exe else cfg.idle_threshold
        )

        if idle >= threshold:
            # Idle began the instant input stopped, not now.
            self.observe(IDLE_EXE, "Idle", now - idle, now)
        else:
            # Coming back from idle, the active stretch began when input resumed.
            boundary = now - idle if (self.current and self.current.exe == IDLE_EXE) else now
            self.observe(exe, title, boundary, now)

    # -- shipping ----------------------------------------------------------

    def spool_append(self, rows: list[dict]) -> None:
        if not rows:
            return
        try:
            if (
                self.cfg.spool_path.exists()
                and self.cfg.spool_path.stat().st_size > self.cfg.spool_max_bytes
            ):
                log(self.cfg, "spool full - dropping oldest half")
                lines = self.cfg.spool_path.read_text(encoding="utf-8").splitlines()
                self.cfg.spool_path.write_text(
                    "\n".join(lines[len(lines) // 2 :]) + "\n", encoding="utf-8"
                )
            with self.cfg.spool_path.open("a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError as exc:
            log(self.cfg, f"spool write failed: {exc}")

    def post(self, segments: list[dict]) -> bool:
        body = json.dumps({"host": self.cfg.host, "segments": segments}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.cfg.server}/api/ingest",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Ingest-Token": self.cfg.token,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return 200 <= resp.status < 300
        except urllib.error.HTTPError as exc:
            log(self.cfg, f"server rejected batch: HTTP {exc.code} {exc.reason}")
            # 4xx means this payload will never be accepted; drop it rather
            # than spooling a poison pill forever.
            return 400 <= exc.code < 500
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            vlog(self.cfg, f"server unreachable: {exc}")
            return False

    def flush(self) -> None:
        # Closed segments hit the spool first, so a crash mid-flush loses nothing.
        self.spool_append(self.pending)
        self.pending = []

        rows: list[dict] = []
        if self.cfg.spool_path.exists():
            try:
                for line in self.cfg.spool_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
            except (OSError, json.JSONDecodeError) as exc:
                log(self.cfg, f"spool read failed: {exc}")
                rows = []

        # The open segment rides along so the dashboard is live. It is never
        # spooled: it will be re-sent on the next flush anyway.
        live = [self.current.payload(closed=False)] if self.current else []

        if not rows and not live:
            return

        ok = True
        for i in range(0, len(rows), 500):
            if not self.post(rows[i : i + 500]):
                ok = False
                break

        if ok:
            try:
                self.cfg.spool_path.unlink(missing_ok=True)
            except OSError:
                pass
            if rows:
                log(self.cfg, f"sent {len(rows)} segment(s)")
            if live:
                self.post(live)
        else:
            log(self.cfg, f"holding {len(rows)} segment(s) in spool - server unreachable")

    # -- main loop ---------------------------------------------------------

    def stop(self, *_args) -> None:
        self.running = False

    def run(self) -> None:
        cfg = self.cfg
        log(cfg, f"collector -> {cfg.server}  (poll {cfg.poll:g}s, flush {cfg.flush:g}s)")
        log(cfg, f"idle after {cfg.idle_threshold:g}s ({cfg.media_idle_threshold:g}s for media apps)")
        if not cfg.token:
            log(cfg, "WARNING: no INGEST_TOKEN set - the server will reject every batch")

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):
                pass

        while self.running:
            try:
                self.tick()
                if time.time() - self.last_flush >= cfg.flush:
                    self.flush()
                    self.last_flush = time.time()
            except Exception as exc:  # never let one bad tick kill the loop
                log(cfg, f"tick error: {exc!r}")
            time.sleep(cfg.poll)

        log(cfg, "shutting down - closing open segment and flushing")
        self.close_current(time.time())
        self.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows activity collector")
    parser.add_argument("--server", help="tracker server base URL")
    parser.add_argument("--token", help="ingest token (matches INGEST_TOKEN on the server)")
    parser.add_argument("--poll", type=float, help="seconds between samples")
    parser.add_argument("--flush", type=float, help="seconds between uploads")
    parser.add_argument("--spool", help="path to the offline spool file")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every segment")
    parser.add_argument(
        "--env",
        default=str(Path(__file__).resolve().parent.parent / ".env"),
        help="path to the project .env (default: ../.env)",
    )
    args = parser.parse_args()

    load_env_file(Path(args.env))

    if not sys.platform.startswith("win"):
        print("This collector uses the Win32 API and only runs on Windows.", file=sys.stderr)
        return 1

    Collector(Config(args)).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
