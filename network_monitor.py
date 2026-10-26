#!/usr/bin/env python3
"""
network_monitor.py - Network + Power monitor for ONE office PC.

WHAT IT DOES (every CHECK_INTERVAL_SECONDS, default 5 minutes)
  1. Pings your router and a few public addresses  -> is the network alive?
  2. Runs an Ookla speed test                      -> Ideal / Low
  3. Writes one row to a local CSV file            -> your permanent record
  4. Copies the row to Supabase                    -> the online dashboard
  5. E-mails you when a problem is CONFIRMED, and again when it is fixed
  6. Notices power cuts: a "heartbeat" file proves the PC was alive; if it
     is old when the monitor starts, the PC was off in between.

HOW TO USE IT
    python network_monitor.py --check     test your settings (do this first!)
    python network_monitor.py             run the monitor
    python network_monitor.py --help      all options

Only the Python standard library is needed (Python 3.9 or newer).

READING THIS FILE: it is split into numbered sections. If you want to learn
how it works, read section 12 (AlertEngine) and section 14 (Monitor) first.
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import smtplib
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Callable, Dict, List, Optional, Tuple

# Force UTF-8 encoding on standard streams to prevent Windows cp1252 charmap errors
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TS_FMT = "%Y-%m-%d %H:%M:%S"
IS_WINDOWS = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Words used in the CSV / database. The dashboard understands exactly these.
IDEAL, LOW, DOWN, TEST_FAILED = "Ideal", "Low", "Totally Down", "Test Failed"
PC_ON, PC_OFF, PC_ASLEEP, PC_STOPPED = "ON", "OFF", "ASLEEP", "MONITOR_STOPPED"
NOT_APPLICABLE = "N/A"

# Exit codes (start_monitor.bat reacts to these)
EXIT_OK, EXIT_CRASH, EXIT_BAD_CONFIG, EXIT_ALREADY_RUNNING = 0, 1, 2, 3


def configure_windows_os_resilience() -> None:
    """On Windows:
    1. Prevents idle system sleep via SetThreadExecutionState while the monitor is active.
    2. Disables Console QuickEdit Mode so clicking inside the console window does not freeze Python.
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        # ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x00000001)
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        logging.info("Windows sleep prevention activated (ES_SYSTEM_REQUIRED).")
    except Exception as exc:
        logging.debug("Could not set Windows execution state: %s", exc)

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h_stdin = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
            # 0x0040 = ENABLE_QUICK_EDIT_MODE, 0x0080 = ENABLE_EXTENDED_FLAGS
            new_mode = (mode.value & ~0x0040) | 0x0080
            kernel32.SetConsoleMode(h_stdin, new_mode)
            logging.info("Windows console QuickEdit mode disabled (click-to-freeze prevented).")
    except Exception as exc:
        logging.debug("Could not adjust console QuickEdit mode: %s", exc)

CSV_COLUMNS = [
    "timestamp",
    "computer_status",
    "ping_status",
    "ping_avg_ms",
    "ping_loss_percent",
    "download_mbps",
    "upload_mbps",
    "idle_latency_ms",
    "speedtest_packet_loss_percent",
    "server",
    "network_status",
    "note",
    "result_url",
]

TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
PLACEHOLDER_HINTS = ("example.com", "your-", "your_", "<")


# =============================================================================
# 1. SMALL HELPERS
# =============================================================================
def now_str() -> str:
    return datetime.now().strftime(TS_FMT)


def parse_ts(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value).strip(), TS_FMT)
    except (ValueError, TypeError):
        return None


def format_seconds(total: float) -> str:
    total = max(0, int(total))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def fmt_duration(start_str: str, end_str: str) -> str:
    start, end = parse_ts(start_str), parse_ts(end_str)
    if not start or not end:
        return "unknown"
    return format_seconds((end - start).total_seconds())


def is_placeholder(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return (not text) or any(hint in text for hint in PLACEHOLDER_HINTS)


def atomic_write_text(path: str, text: str) -> None:
    """Write a file so that a power cut can never leave it half-written:
    write to a temporary file, force it to disk, then swap it into place."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    last_error: Optional[BaseException] = None
    for attempt in range(5):
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            return
        except OSError as exc:  # e.g. antivirus briefly locking the file
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last_error  # type: ignore[misc]


def short(text: Any, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# =============================================================================
# 2. SETTINGS  (read from config.py, checked before the monitor starts)
# =============================================================================
class Settings:
    """All settings in one place, with safe defaults and a validate() method."""

    def __init__(self, cfg: Any, environ: Optional[Dict[str, str]] = None) -> None:
        env = os.environ if environ is None else environ
        self._init_errors: List[str] = []

        def get(name: str, default: Any = None) -> Any:
            return getattr(cfg, name, default)

        def number(name: str, default: float, cast: Callable[[Any], Any] = float) -> Any:
            raw = get(name, default)
            try:
                return cast(raw)
            except (TypeError, ValueError):
                self._init_errors.append(f"{name} must be a number (found {raw!r})")
                return cast(default)

        # --- folders / files
        base = get("BASE_DIR", BASE_DIR)
        self.log_dir = get("LOG_DIRECTORY", os.path.join(base, "logs"))
        self.csv_file = get("CSV_LOG_FILE", os.path.join(self.log_dir, "network_speed_log.csv"))
        self.app_log_file = get("APP_LOG_FILE", os.path.join(self.log_dir, "network_monitor.log"))
        self.state_file = get("STATE_FILE", os.path.join(self.log_dir, "monitor_state.json"))
        self.heartbeat_file = get("HEARTBEAT_FILE", os.path.join(self.log_dir, "heartbeat.txt"))
        self.lock_file = os.path.join(self.log_dir, "monitor.lock")
        self.supabase_outbox_file = get(
            "SUPABASE_OUTBOX_FILE", os.path.join(self.log_dir, "supabase_outbox.jsonl")
        )
        self.supabase_rejected_file = os.path.join(self.log_dir, "supabase_rejected.jsonl")
        self.email_outbox_file = os.path.join(self.log_dir, "email_outbox.jsonl")
        self.csv_pending_file = os.path.join(self.log_dir, "csv_pending.jsonl")

        # --- office + speed rules
        self.office_name = str(get("OFFICE_NAME", "New Office")).strip() or "Office"
        self.expected_speed = number("EXPECTED_SPEED", 100.0)
        self.alert_threshold = number("ALERT_THRESHOLD", 85.0)
        upload_threshold = get("ALERT_THRESHOLD_UPLOAD", None)
        self.alert_threshold_upload = (
            None if upload_threshold in (None, "") else number("ALERT_THRESHOLD_UPLOAD", 0.0)
        )
        self.check_interval = number("CHECK_INTERVAL_SECONDS", 300, int)
        # RECOVERY_RECHECK_SECONDS is the name used by older versions
        self.recheck_seconds = number(
            "RECHECK_SECONDS", get("RECOVERY_RECHECK_SECONDS", 120), int
        )
        self.confirm_checks = number("CONFIRM_CHECKS", 2, int)

        # --- ping
        self.ping_target = str(get("PING_TARGET", "192.168.51.1")).strip()
        targets = get("INTERNET_PING_TARGETS", ["1.1.1.1", "8.8.8.8"])
        self.internet_targets = [str(t).strip() for t in (targets or []) if str(t).strip()]
        self.ping_count = number("PING_COUNT", 4, int)

        # --- power
        self.heartbeat_seconds = number("HEARTBEAT_SECONDS", 30, int)
        self.off_gap_seconds = number("OFF_GAP_SECONDS", 120, int)
        self.network_wait = number("STARTUP_NETWORK_WAIT_SECONDS", 180, int)

        # --- speedtest
        self.speedtest_exe = str(get("SPEEDTEST_EXE", os.path.join(base, "speedtest.exe")))
        self.speedtest_timeout = number("SPEEDTEST_TIMEOUT_SECONDS", 120, int)

        # --- e-mail
        self.email_enabled = bool(get("EMAIL_ENABLED", True))
        self.smtp_server = str(get("SMTP_SERVER", "")).strip()
        self.smtp_port = number("SMTP_PORT", 587, int)
        self.smtp_encryption = str(get("SMTP_ENCRYPTION", "starttls")).strip().lower()
        self.smtp_timeout = number("SMTP_TIMEOUT_SECONDS", 30, int)
        self.email_user = str(get("EMAIL_USERNAME", "")).strip()
        self.email_password = str(
            env.get("MONITOR_EMAIL_PASSWORD") or get("EMAIL_PASSWORD", "")
        )
        self.alert_to = [str(x).strip() for x in (get("ALERT_TO", []) or []) if str(x).strip()]
        self.alert_cc = [str(x).strip() for x in (get("ALERT_CC", []) or []) if str(x).strip()]
        self.send_recovery_email = bool(get("SEND_RECOVERY_EMAIL", True))
        self.email_max_age_hours = number("EMAIL_MAX_AGE_HOURS", 24, float)
        dashboard = str(get("DASHBOARD_URL", "")).strip()
        self.dashboard_url = "" if is_placeholder(dashboard) else dashboard

        # --- supabase
        self.supabase_url = str(get("SUPABASE_URL", "")).strip().rstrip("/")
        self.supabase_key = str(env.get("MONITOR_SUPABASE_KEY") or get("SUPABASE_KEY", "")).strip()
        self.supabase_table = str(get("SUPABASE_TABLE", "network_log")).strip()
        self.supabase_tz_offset = str(get("SUPABASE_TZ_OFFSET", "") or "").strip()

    # -- derived values -------------------------------------------------------
    @property
    def supabase_enabled(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    @property
    def recipients(self) -> List[str]:
        return self.alert_to + self.alert_cc

    # -- validation -----------------------------------------------------------
    def validate(self) -> Tuple[List[str], List[str]]:
        """Return (errors, warnings). Errors stop the monitor from starting."""
        errors = list(self._init_errors)
        warnings: List[str] = []

        if self.check_interval < 60:
            errors.append("CHECK_INTERVAL_SECONDS must be at least 60.")
        if self.recheck_seconds < 30:
            errors.append("RECHECK_SECONDS must be at least 30.")
        if self.confirm_checks < 1:
            errors.append("CONFIRM_CHECKS must be at least 1.")
        if self.ping_count < 1:
            errors.append("PING_COUNT must be at least 1.")
        if self.alert_threshold <= 0:
            errors.append("ALERT_THRESHOLD must be greater than 0.")
        if not self.ping_target:
            errors.append("PING_TARGET (your router address) is empty.")
        if not self.internet_targets:
            errors.append("INTERNET_PING_TARGETS is empty. Use for example ['1.1.1.1', '8.8.8.8'].")
        if self.alert_threshold > self.expected_speed:
            warnings.append("ALERT_THRESHOLD is higher than EXPECTED_SPEED - you will get constant alerts.")

        if self.email_enabled:
            if is_placeholder(self.smtp_server):
                errors.append("SMTP_SERVER still has the example value. Fix it or set EMAIL_ENABLED = False.")
            if self.smtp_encryption not in ("starttls", "ssl"):
                errors.append('SMTP_ENCRYPTION must be "starttls" (port 587) or "ssl" (port 465).')
            if is_placeholder(self.email_user):
                errors.append("EMAIL_USERNAME still has the example value.")
            if is_placeholder(self.email_password):
                errors.append("EMAIL_PASSWORD still has the example value.")
            real = [r for r in self.recipients if "@" in r and not is_placeholder(r)]
            if not real:
                errors.append("ALERT_TO has no real e-mail address.")

        if self.supabase_url or self.supabase_key:
            if not (self.supabase_url and self.supabase_key):
                errors.append("Set BOTH SUPABASE_URL and SUPABASE_KEY, or leave both empty.")
            elif is_placeholder(self.supabase_url) or is_placeholder(self.supabase_key):
                errors.append("SUPABASE_URL / SUPABASE_KEY still have the example values.")
            elif not self.supabase_url.startswith("https://"):
                errors.append("SUPABASE_URL must start with https://")
            if not TABLE_NAME_RE.match(self.supabase_table):
                errors.append("SUPABASE_TABLE may only contain letters, digits and underscores.")
            else:
                office_old = "old" in self.office_name.lower()
                table_old = "old" in self.supabase_table.lower()
                if office_old != table_old:
                    warnings.append(
                        f"OFFICE_NAME is '{self.office_name}' but SUPABASE_TABLE is "
                        f"'{self.supabase_table}'. Is that the right pair?"
                    )
        return errors, warnings


# =============================================================================
# 3. SAFE FILE STORAGE: a queue that survives power cuts
# =============================================================================
class PersistentQueue:
    """A list of items kept in a file (one JSON object per line).
    Every change rewrites the file safely, so nothing is lost on a power cut.
    Used for e-mails waiting to be sent and rows waiting for Supabase."""

    def __init__(self, path: str, max_items: int = 50000) -> None:
        self.path = path
        self.max_items = max_items
        self._lock = threading.RLock()
        self._items: List[Dict[str, Any]] = []
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # a line damaged by a power cut: skip it
            if isinstance(obj, dict) and "id" in obj and "data" in obj:
                item = {"id": int(obj["id"]), "data": obj["data"]}
            else:  # old file format from the previous version: wrap it
                item = {"id": self._next_id, "data": obj}
            self._items.append(item)
            self._next_id = max(self._next_id, item["id"] + 1)

    def _save(self) -> None:
        try:
            if self._items:
                text = "".join(json.dumps(i, ensure_ascii=False) + "\n" for i in self._items)
                atomic_write_text(self.path, text)
            elif os.path.exists(self.path):
                os.remove(self.path)
        except OSError as exc:
            logging.error("Could not save %s: %s", os.path.basename(self.path), exc)

    def put(self, data: Dict[str, Any]) -> int:
        with self._lock:
            item = {"id": self._next_id, "data": data}
            self._next_id += 1
            self._items.append(item)
            if len(self._items) > self.max_items:
                del self._items[: len(self._items) - self.max_items]
            self._save()
            return item["id"]

    def peek(self, count: int) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._items[:count])

    def remove_ids(self, ids: List[int]) -> None:
        wanted = set(ids)
        with self._lock:
            self._items = [i for i in self._items if i["id"] not in wanted]
            self._save()

    def remove_where(self, predicate: Callable[[Dict[str, Any]], bool]) -> int:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if not predicate(i)]
            if len(self._items) != before:
                self._save()
            return before - len(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


# =============================================================================
# 4. LOGGING, SAVED STATE, SINGLE-INSTANCE LOCK
# =============================================================================
def setup_logging(settings: Settings) -> None:
    os.makedirs(settings.log_dir, exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    handlers: List[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            settings.app_log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
    ]
    if sys.stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=handlers,
        force=True,
    )


def load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    problem = state.get("problem")
    if isinstance(problem, dict):  # upgrade a problem saved by the old version
        problem.setdefault("bad_count", 1)
        problem.setdefault("confirmed", bool(problem.get("alert_sent")))
        problem.setdefault("alerted_type", problem.get("type") if problem.get("alert_sent") else None)
    else:
        problem = None
    return {"problem": problem, "clean_stop_at": state.get("clean_stop_at")}


def save_state(path: str, state: Dict[str, Any]) -> None:
    try:
        atomic_write_text(path, json.dumps(state, indent=2))
    except OSError as exc:
        logging.error("Could not save the state file: %s", exc)


class SingleInstance:
    """Stops a second copy of the monitor from running (which would send
    every alert twice). The lock is released by Windows automatically if the
    program dies, so it can never get 'stuck'."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if IS_WINDOWS:
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if not self._handle:
            return
        try:
            if IS_WINDOWS:
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except OSError:
            pass
        self._handle.close()
        self._handle = None


# =============================================================================
# 5. POWER: heartbeat + gap detection
# =============================================================================
class Heartbeat(threading.Thread):
    """Writes the current time to a file every few seconds.
    The newest value left behind tells us when the PC was last alive.
    It also notices if the PC fell asleep while the monitor kept running."""

    def __init__(self, settings: Settings, stop_event: threading.Event) -> None:
        super().__init__(name="heartbeat", daemon=True)
        self.path = settings.heartbeat_file
        self.interval = settings.heartbeat_seconds
        self.gap_seconds = settings.off_gap_seconds
        self.stop_event = stop_event
        self._lock = threading.Lock()
        self._gaps: List[Tuple[str, str]] = []
        self._failing = False

    def beat_once(self) -> None:
        atomic_write_text(self.path, now_str())

    def pop_gaps(self) -> List[Tuple[str, str]]:
        with self._lock:
            gaps, self._gaps = self._gaps, []
        return gaps

    def run(self) -> None:
        last_tick = time.time()
        while not self.stop_event.is_set():
            try:
                now = time.time()
                if now - last_tick > self.gap_seconds:
                    with self._lock:
                        self._gaps.append(
                            (
                                datetime.fromtimestamp(last_tick).strftime(TS_FMT),
                                datetime.fromtimestamp(now).strftime(TS_FMT),
                            )
                        )
                last_tick = now
                self.beat_once()
                self._failing = False
            except Exception as exc:  # never let this thread die
                if not self._failing:
                    logging.warning("Could not write the heartbeat file: %s", exc)
                self._failing = True
            self.stop_event.wait(self.interval)


def read_last_csv_timestamp(path: str) -> Optional[datetime]:
    """Timestamp of the newest row in the CSV (reads only the end of the file)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - 8192))
            tail = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        first = line.split(",", 1)[0].strip().strip('"').lstrip("﻿")
        stamp = parse_ts(first)
        if stamp:
            return stamp
    return None


def read_last_alive(settings: Settings) -> Optional[datetime]:
    """When was the PC last known to be running? Uses the heartbeat file AND the
    newest CSV row, so a damaged heartbeat file after a power cut cannot hide
    the outage."""
    candidates: List[datetime] = []
    try:
        with open(settings.heartbeat_file, "r", encoding="utf-8") as handle:
            stamp = parse_ts(handle.read())
            if stamp:
                candidates.append(stamp)
    except OSError:
        pass
    csv_stamp = read_last_csv_timestamp(settings.csv_file)
    if csv_stamp:
        candidates.append(csv_stamp)
    return max(candidates) if candidates else None


def detect_gap(
    settings: Settings, state: Dict[str, Any], now: Optional[datetime] = None
) -> Optional[Dict[str, str]]:
    """Was the monitor not running for a while before this start?
    Returns {"kind": "OFF" | "STOPPED", "off_from": ..., "off_to": ...} or None."""
    now = now or datetime.now()
    last_alive = read_last_alive(settings)
    if last_alive is None or now <= last_alive:
        return None
    if (now - last_alive).total_seconds() <= settings.off_gap_seconds:
        return None
    kind, off_from = PC_OFF, last_alive
    stopped_at = parse_ts(state.get("clean_stop_at"))
    if stopped_at and (stopped_at - last_alive).total_seconds() >= -settings.off_gap_seconds:
        kind, off_from = PC_STOPPED, stopped_at  # the monitor was closed on purpose
    return {
        "kind": kind,
        "off_from": off_from.strftime(TS_FMT),
        "off_to": now.strftime(TS_FMT),
    }


# =============================================================================
# 6. PING
# =============================================================================
def build_ping_command(target: str, count: int, timeout_ms: int = 1500) -> List[str]:
    if IS_WINDOWS:
        return ["ping", "-n", str(count), "-w", str(timeout_ms), target]
    return ["ping", "-c", str(count), "-W", str(max(1, timeout_ms // 1000)), target]


def parse_ping_output(output: str, count: int) -> Dict[str, Any]:
    """Count real replies (lines with TTL) and average their times.
    Works for Windows and Linux, and does not depend on the Windows language."""
    reply_lines = [ln for ln in output.splitlines() if re.search(r"ttl=", ln, re.IGNORECASE)]
    replies = len(reply_lines)
    times = []
    for line in reply_lines:
        found = re.search(r"[=<]\s*(\d+(?:\.\d+)?)\s*ms", line, re.IGNORECASE)
        if found:
            times.append(float(found.group(1)))
    loss = max(0.0, (count - replies) / count * 100.0)
    return {
        "status": "Reachable" if replies > 0 else "Unreachable",
        "avg_ms": round(statistics.mean(times)) if times else None,
        "loss": loss,
    }


def run_ping(target: str, count: int = 4) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            build_ping_command(target, count),
            capture_output=True,
            text=True,
            timeout=count * 3 + 10,
            creationflags=NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"status": "Unreachable", "avg_ms": None, "loss": 100.0}
    return parse_ping_output(completed.stdout or "", count)


def check_internet(targets: List[str]) -> Dict[str, Any]:
    """The internet counts as up if ANY public address answers."""
    for target in targets:
        result = run_ping(target, 2)
        if result["status"] == "Reachable":
            return {"ok": True, "target": target, "avg_ms": result["avg_ms"]}
    return {"ok": False, "target": None, "avg_ms": None}


# =============================================================================
# 7. SPEED TEST (official Ookla CLI)
# =============================================================================
class SpeedtestError(Exception):
    pass


def find_speedtest(settings: Settings) -> str:
    candidates = [settings.speedtest_exe, "speedtest.exe", os.path.join(BASE_DIR, "speedtest.exe")]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    found = shutil.which("speedtest.exe") or shutil.which("speedtest")
    if found and "scripts" not in os.path.normcase(found).split(os.sep):
        return found
    raise SpeedtestError(
        "speedtest.exe not found. Download the Ookla Speedtest CLI and put speedtest.exe next to this script."
    )


def parse_speedtest_json(text: str) -> Dict[str, Any]:
    result: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "result" or ("download" in obj and "upload" in obj):
            result = obj
        elif obj.get("type") == "log" and obj.get("level") == "error":
            error_message = obj.get("message")
    if result is None:
        raise SpeedtestError(error_message or "Speedtest printed no result.")

    def mbps(section: str) -> float:
        bandwidth = (result.get(section) or {}).get("bandwidth")
        if bandwidth is None:
            raise SpeedtestError(f"Speedtest result has no {section} speed.")
        return float(bandwidth) * 8 / 1_000_000  # bytes per second -> megabits per second

    latency = (result.get("ping") or {}).get("latency")
    server = result.get("server") or {}
    server_text = " - ".join(x for x in (server.get("name"), server.get("location")) if x)
    packet_loss = result.get("packetLoss")
    return {
        "download": mbps("download"),
        "upload": mbps("upload"),
        "idle_latency": float(latency) if latency is not None else None,
        "packet_loss": float(packet_loss) if packet_loss is not None else None,
        "server": server_text,
        "result_url": (result.get("result") or {}).get("url") or "",
    }


def parse_speedtest_text(text: str) -> Dict[str, Any]:
    """Fallback for a speedtest.exe that does not support --format=json."""

    def number(pattern: str, required: bool = True) -> Optional[float]:
        found = re.search(pattern, text, re.IGNORECASE)
        if not found:
            if required:
                raise SpeedtestError("Could not read the speedtest output.")
            return None
        return float(found.group(1))

    server = re.search(r"Server:\s*(.+?)(?:\s+\(id:\s*\d+\))?\s*$", text, re.IGNORECASE | re.MULTILINE)
    url = re.search(r"Result URL:\s*(https?://\S+)", text, re.IGNORECASE)
    return {
        "download": number(r"Download:\s*([0-9]+(?:\.[0-9]+)?)\s*Mbps"),
        "upload": number(r"Upload:\s*([0-9]+(?:\.[0-9]+)?)\s*Mbps"),
        "idle_latency": number(r"Idle Latency:\s*([0-9]+(?:\.[0-9]+)?)\s*ms", required=False),
        "packet_loss": number(r"Packet Loss:\s*([0-9]+(?:\.[0-9]+)?)\s*%", required=False),
        "server": server.group(1).strip() if server else "",
        "result_url": url.group(1) if url else "",
    }


def run_speedtest(settings: Settings) -> Dict[str, Any]:
    exe = find_speedtest(settings)
    logging.info("Running the Ookla speed test...")
    completed = subprocess.run(
        [exe, "--accept-license", "--accept-gdpr", "--format=json"],
        capture_output=True,
        text=True,
        timeout=settings.speedtest_timeout,
        creationflags=NO_WINDOW,
    )
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    try:
        return parse_speedtest_json(output)
    except SpeedtestError as json_error:
        if completed.returncode != 0:
            raise SpeedtestError(
                f"Speedtest failed (exit {completed.returncode}): {short(json_error, 120)}"
            )
        try:
            return parse_speedtest_text(output)
        except SpeedtestError:
            raise json_error


# =============================================================================
# 8. THE CSV FILE (your permanent record)
# =============================================================================
class CsvLog:
    """Appends rows to the CSV. If the file is locked (for example it is open
    in Excel) the row waits in a side file and is added later. Rows are forced
    to disk, so a power cut cannot lose them."""

    def __init__(self, settings: Settings) -> None:
        self.path = settings.csv_file
        self.pending = PersistentQueue(settings.csv_pending_file)
        self._lock = threading.Lock()

    def ensure_header(self) -> None:
        """Run once at start-up. Creates the file, or upgrades an old layout."""
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
                self._write_fresh()
                return
            with open(self.path, "r", newline="", encoding="utf-8-sig") as handle:
                header = next(csv.reader(handle), [])
            if header == CSV_COLUMNS:
                self._ensure_trailing_newline()
                return
            if header and set(header) <= set(CSV_COLUMNS) and "timestamp" in header:
                self._upgrade_columns()
                return
            backup = f"{self.path}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.old"
            os.rename(self.path, backup)
            logging.warning("The CSV had an unknown layout. It was moved to %s", backup)
            self._write_fresh()
        except OSError as exc:
            logging.error("Could not prepare the CSV file (%s). New rows will wait in csv_pending.jsonl.", exc)

    def _write_fresh(self) -> None:
        with open(self.path, "w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow(CSV_COLUMNS)

    def _ensure_trailing_newline(self) -> None:
        # A power cut can leave a half-written last line without a line break.
        with open(self.path, "rb+") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                return
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) not in (b"\n", b"\r"):
                handle.write(b"\r\n")

    def _upgrade_columns(self) -> None:
        with open(self.path, "r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        shutil.copy2(self.path, self.path + ".before-upgrade.bak")
        with open(self.path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(CSV_COLUMNS)
            for row in rows:
                writer.writerow([row.get(col, "") or "" for col in CSV_COLUMNS])
        logging.info("CSV upgraded to the current column layout (backup kept).")

    def _write_rows(self, rows: List[Dict[str, Any]]) -> None:
        with open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            for row in rows:
                writer.writerow([row.get(col, "") for col in CSV_COLUMNS])
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, row: Dict[str, Any]) -> bool:
        with self._lock:
            waiting = self.pending.peek(1000)
            rows = [item["data"] for item in waiting] + [row]
            for attempt in range(3):
                try:
                    self._write_rows(rows)
                    if waiting:
                        self.pending.remove_ids([item["id"] for item in waiting])
                    return True
                except OSError as exc:  # usually: the CSV is open in Excel
                    logging.warning("CSV is locked or unavailable (%s). Retrying...", exc)
                    time.sleep(2)
            self.pending.put(row)
            logging.error("CSV not writable. The row is kept safely and will be added later.")
            return False


# =============================================================================
# 9. SUPABASE UPLOAD (runs in the background, never blocks the monitor)
# =============================================================================
def supabase_key_kind(key: str) -> str:
    if key.startswith("sb_secret_"):
        return "secret (full access)"
    if key.startswith("sb_publishable_"):
        return "publishable"
    try:
        payload = key.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload)).get("role", "unknown"))
    except Exception:
        return "unknown"


def supabase_headers(key: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {"apikey": key, "Content-Type": "application/json"}
    if not key.startswith("sb_"):  # old-style JWT keys also go in the Authorization header
        headers["Authorization"] = f"Bearer {key}"
    headers.update(extra or {})
    return headers


def ts_to_iso(timestamp: str, tz_override: str = "") -> str:
    """'2026-10-25 10:50:00' (PC local time) -> '2026-10-25T10:50:00+05:30'."""
    if tz_override:
        return timestamp.replace(" ", "T") + tz_override
    return datetime.strptime(timestamp, TS_FMT).astimezone().isoformat(timespec="seconds")


def _number_or_none(value: Any) -> Optional[float]:
    try:
        return None if value in ("", None) else float(value)
    except (TypeError, ValueError):
        return None


def to_record(settings: Settings, row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ts": ts_to_iso(row["timestamp"], settings.supabase_tz_offset),
        "computer_status": row.get("computer_status", ""),
        "ping_status": row.get("ping_status", ""),
        "ping_avg_ms": _number_or_none(row.get("ping_avg_ms")),
        "ping_loss_percent": _number_or_none(row.get("ping_loss_percent")),
        "download_mbps": _number_or_none(row.get("download_mbps")),
        "upload_mbps": _number_or_none(row.get("upload_mbps")),
        "idle_latency_ms": _number_or_none(row.get("idle_latency_ms")),
        "speedtest_packet_loss_percent": _number_or_none(row.get("speedtest_packet_loss_percent")),
        "server": row.get("server", ""),
        "network_status": row.get("network_status", ""),
        "note": row.get("note", ""),
        "result_url": row.get("result_url", ""),
    }


class PostResult:
    """code 0 means 'could not reach Supabase at all'."""

    def __init__(self, code: int, body: str = "") -> None:
        self.code = code
        self.body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.code < 300


class SupabaseClient:
    def __init__(self, url: str, key: str, table: str, timeout: int = 15) -> None:
        self.url, self.key, self.table, self.timeout = url.rstrip("/"), key, table, timeout

    def _call(self, request: urllib.request.Request) -> PostResult:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
                return PostResult(response.status, body[:300])
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                body = ""
            return PostResult(exc.code, body)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return PostResult(0, str(exc))

    def post(self, records: List[Dict[str, Any]]) -> PostResult:
        url = f"{self.url}/rest/v1/{self.table}?on_conflict=ts,computer_status"
        headers = supabase_headers(
            self.key, {"Prefer": "resolution=ignore-duplicates,return=minimal"}
        )
        request = urllib.request.Request(
            url, data=json.dumps(records).encode("utf-8"), headers=headers, method="POST"
        )
        return self._call(request)

    def sample(self) -> PostResult:
        url = f"{self.url}/rest/v1/{self.table}?select=ts&limit=1"
        request = urllib.request.Request(url, headers=supabase_headers(self.key), method="GET")
        return self._call(request)


class FlushOutcome:
    def __init__(self, sent: int = 0, rejected: int = 0, problem: str = "", detail: str = "") -> None:
        self.sent, self.rejected, self.problem, self.detail = sent, rejected, problem, detail


def _is_temporary(code: int) -> bool:
    return code == 0 or code in (408, 425, 429) or code >= 500


def flush_once(
    queue: PersistentQueue, client: SupabaseClient, rejected_path: str, batch_size: int = 500
) -> FlushOutcome:
    """Send everything waiting in the queue.
    - Internet / server trouble  -> keep everything, try again later.
    - Key / table / setup trouble -> keep everything, tell the user loudly.
    - One truly invalid row      -> set it aside in supabase_rejected.jsonl so
                                    it can never block the other rows."""
    outcome = FlushOutcome()
    while True:
        items = queue.peek(batch_size)
        if not items:
            return outcome
        result = client.post([item["data"] for item in items])
        if result.ok:
            queue.remove_ids([item["id"] for item in items])
            outcome.sent += len(items)
            continue
        if _is_temporary(result.code):
            outcome.problem, outcome.detail = "network", f"HTTP {result.code} {result.body}"
            return outcome
        if result.code in (401, 403, 404):
            outcome.problem, outcome.detail = "config", f"HTTP {result.code} {result.body}"
            return outcome
        # 400 / 409 / 422: a bad row, or a setup problem. Find out which.
        failed: List[Dict[str, Any]] = []
        any_success = outcome.sent > 0
        consecutive = 0
        for item in items:
            single = client.post([item["data"]])
            if single.ok:
                queue.remove_ids([item["id"]])
                outcome.sent += 1
                if failed:  # a later row worked, so earlier failures were bad rows
                    _reject(queue, failed, rejected_path, outcome)
                    failed = []
                any_success, consecutive = True, 0
            elif _is_temporary(single.code) or single.code in (401, 403, 404):
                outcome.problem = "network" if _is_temporary(single.code) else "config"
                outcome.detail = f"HTTP {single.code} {single.body}"
                return outcome
            else:
                failed.append(item)
                consecutive += 1
                outcome.detail = f"HTTP {single.code} {single.body}"
                if not any_success and consecutive >= 3:
                    outcome.problem = "config"  # everything fails: the setup is wrong
                    return outcome
        if failed:
            if any_success:
                _reject(queue, failed, rejected_path, outcome)
            else:
                outcome.problem = "config"
                return outcome


def _reject(
    queue: PersistentQueue, items: List[Dict[str, Any]], path: str, outcome: FlushOutcome
) -> None:
    try:
        with open(path, "a", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps({"rejected_at": now_str(), "row": item["data"]}) + "\n")
    except OSError as exc:
        logging.error("Could not write %s: %s", path, exc)
        return
    queue.remove_ids([item["id"] for item in items])
    outcome.rejected += len(items)
    logging.error("Supabase refused %d row(s). They stay in the CSV and in %s", len(items), path)


class Uploader(threading.Thread):
    """Sends queued rows to Supabase as soon as they appear, and keeps
    retrying (with growing pauses) while the internet is down."""

    def __init__(
        self,
        settings: Settings,
        queue: PersistentQueue,
        client: SupabaseClient,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="uploader", daemon=True)
        self.s, self.queue, self.client, self.stop_event = settings, queue, client, stop_event
        self.wake = threading.Event()

    def run(self) -> None:
        backoff, next_try = 0.0, 0.0
        while not self.stop_event.is_set():
            self.wake.wait(timeout=10)
            self.wake.clear()
            if self.stop_event.is_set():
                break
            try:
                if len(self.queue) == 0:
                    backoff = 0.0
                    continue
                if time.time() < next_try:
                    continue
                outcome = flush_once(self.queue, self.client, self.s.supabase_rejected_file)
                if outcome.sent:
                    logging.info("Supabase: uploaded %d row(s).", outcome.sent)
                if not outcome.problem:
                    backoff = 0.0
                    continue
                if outcome.problem == "network":
                    backoff = min(max(10.0, backoff * 2), 300.0)
                    logging.warning(
                        "Supabase not reachable (%s). %d row(s) are safe in the queue; retry in %ds.",
                        short(outcome.detail, 100), len(self.queue), backoff,
                    )
                else:
                    backoff = min(max(60.0, backoff * 2), 900.0)
                    logging.error(
                        "Supabase upload is blocked (%s). Check SUPABASE_URL, SUPABASE_KEY and that "
                        "supabase_setup.sql was run for table '%s'. Rows are kept safely.",
                        short(outcome.detail, 160), self.s.supabase_table,
                    )
                next_try = time.time() + backoff
            except Exception:
                logging.exception("Uploader error (will keep going)")


# =============================================================================
# 10. E-MAIL (queued, sent in the background, retried until delivered)
# =============================================================================
def _fmt(value: Any, suffix: str = "") -> str:
    return "-" if value in ("", None) else f"{value}{suffix}"


def _html_card(s: Settings, title, title_color, badge, badge_bg, badge_color, details, stats) -> str:
    esc = html.escape
    detail_rows = "".join(
        f'<tr><td style="padding:4px 0; color:#64748b; font-size:14px; width:130px; vertical-align:top;">{esc(str(k))}:</td>'
        f'<td style="padding:4px 0; color:#0f172a; font-weight:600; font-size:14px;">{esc(str(v))}</td></tr>'
        for k, v in details.items()
    )
    stat_cols = "".join(
        f'<td style="background:#f1f5f9; padding:12px; border-radius:8px; text-align:center; width:33%;">'
        f'<div style="font-size:11px; color:#64748b; font-weight:600; text-transform:uppercase;">{esc(str(k))}</div>'
        f'<div style="font-size:17px; color:#0f172a; font-weight:700; margin-top:4px;">{esc(str(v))}</div></td>'
        for k, v in stats.items()
    )
    button = ""
    if s.dashboard_url:
        button = (
            '<div style="margin-top:20px; text-align:center;">'
            f'<a href="{esc(s.dashboard_url, quote=True)}" style="background:#0284c7; color:#ffffff; '
            'text-decoration:none; padding:10px 22px; border-radius:6px; font-weight:600; '
            'font-size:14px; display:inline-block;">View Live Dashboard &rarr;</a></div>'
        )
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0; padding:16px; background-color:#f8fafc; font-family:-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
  <!--DELAY-->
  <div style="max-width:540px; margin:0 auto; background:#ffffff; border:1px solid #e2e8f0; border-radius:10px; overflow:hidden;">
    <div style="padding:16px 20px; border-bottom:1px solid #f1f5f9;">
      <table style="width:100%;"><tr>
        <td><h2 style="margin:0; font-size:18px; color:{title_color};">{esc(title)}</h2></td>
        <td style="text-align:right;"><span style="background:{badge_bg}; color:{badge_color}; padding:4px 10px; border-radius:9999px; font-size:11px; font-weight:700;">{esc(badge)}</span></td>
      </tr></table>
    </div>
    <div style="padding:18px 20px;">
      <table style="width:100%; border-collapse:collapse; margin-bottom:16px;">{detail_rows}</table>
      <table style="width:100%; border-collapse:separate; border-spacing:8px 0; margin-bottom:10px;"><tr>{stat_cols}</tr></table>
      {button}
    </div>
    <div style="padding:10px 20px; background:#f8fafc; border-top:1px solid #f1f5f9; text-align:center; font-size:12px; color:#94a3b8;">
      Network &amp; Power Monitor &bull; {esc(s.office_name)}
    </div>
  </div>
</body>
</html>"""


def _text_block(s: Settings, header: List[str], row: Dict[str, Any]) -> str:
    lines = list(header)
    lines += [
        "",
        "Current performance:",
        f"  Download: {_fmt(row.get('download_mbps'), ' Mbps')}",
        f"  Upload:   {_fmt(row.get('upload_mbps'), ' Mbps')}",
        f"  Ping to router ({s.ping_target}): {_fmt(row.get('ping_avg_ms'), ' ms')}",
    ]
    if s.dashboard_url:
        lines += ["", f"Live dashboard: {s.dashboard_url}"]
    return "\n".join(lines)


def build_alert_email(s: Settings, problem: Dict[str, Any], row: Dict[str, Any]) -> Tuple[str, str, str]:
    is_down = problem["type"] == DOWN
    emoji, label = ("🔴", "Totally Down") if is_down else ("⚠️", "Low Speed")
    since = (parse_ts(problem["start"]) or datetime.now()).strftime("%I:%M %p").lstrip("0")
    subject = f"[{s.office_name}] {emoji} {label.upper()} (since {since})"
    cause = row.get("note") or ""
    header = [
        f"Location: {s.office_name}",
        f"Status:   {label} (target {s.expected_speed:.0f} Mbps, alert below {s.alert_threshold:.0f} Mbps)",
        f"Started:  {problem['start']}  (confirmed after {problem.get('bad_count', 1)} checks)",
    ]
    if cause:
        header.append(f"Cause:    {cause}")
    details = {
        "Location": s.office_name,
        "Status": f"{label} (alert below {s.alert_threshold:.0f} Mbps)",
        "Started": problem["start"],
        "Confirmed": f"after {problem.get('bad_count', 1)} checks in a row",
    }
    if cause:
        details["Cause"] = cause
    stats = {
        "Download": _fmt(row.get("download_mbps"), " Mbps"),
        "Upload": _fmt(row.get("upload_mbps"), " Mbps"),
        "Ping": _fmt(row.get("ping_avg_ms"), " ms"),
    }
    card = _html_card(
        s, f"{emoji} {label} detected", "#b91c1c" if is_down else "#b45309", label.upper(),
        "#fee2e2" if is_down else "#fef3c7", "#991b1b" if is_down else "#92400e", details, stats,
    )
    return subject, _text_block(s, header, row), card


def build_recovery_email(s: Settings, problem: Dict[str, Any], row: Dict[str, Any]) -> Tuple[str, str, str]:
    duration = fmt_duration(problem["start"], row["timestamp"])
    subject = f"[{s.office_name}] 🟢 RECOVERED after {duration}"
    header = [
        f"Location:  {s.office_name}",
        "Status:    Back to normal (Ideal)",
        f"Started:   {problem['start']}  ({problem['type']})",
        f"Recovered: {row['timestamp']}",
        f"Duration:  {duration}",
    ]
    details = {
        "Location": s.office_name,
        "Status": "Back to normal (Ideal)",
        "Problem": problem["type"],
        "Started": problem["start"],
        "Recovered": row["timestamp"],
        "Duration": duration,
    }
    stats = {
        "Download": _fmt(row.get("download_mbps"), " Mbps"),
        "Upload": _fmt(row.get("upload_mbps"), " Mbps"),
        "Ping": _fmt(row.get("ping_avg_ms"), " ms"),
    }
    card = _html_card(s, "🟢 Network recovered", "#047857", "RESOLVED", "#d1fae5", "#065f46", details, stats)
    return subject, _text_block(s, header, row), card


def build_power_email(s: Settings, gap: Dict[str, str], row: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str]:
    duration = fmt_duration(gap["off_from"], gap["off_to"])
    subject = f"[{s.office_name}] ⚡ POWER RESTORED - PC was off for {duration}"
    header = [
        f"Location:     {s.office_name}",
        "Event:        The PC was OFF (power cut or shutdown) and is running again",
        f"Last seen ON: {gap['off_from']}",
        f"Back online:  {gap['off_to']}",
        f"Total OFF:    {duration}",
    ]
    details = {
        "Location": s.office_name,
        "Event": "PC was OFF (power cut or shutdown)",
        "Last seen ON": gap["off_from"],
        "Back online": gap["off_to"],
        "Total OFF": duration,
    }
    stats = {"Download": "-", "Upload": "-", "Ping": "-"}
    card = _html_card(s, "⚡ Power restored", "#334155", "ONLINE", "#e0f2fe", "#0369a1", details, stats)
    text = "\n".join(header + (["", f"Live dashboard: {s.dashboard_url}"] if s.dashboard_url else []))
    return subject, text, card


def build_test_email(s: Settings) -> Tuple[str, str, str]:
    subject = f"[{s.office_name}] ✅ Test e-mail from the Network Monitor"
    text = "If you can read this, e-mail alerts are working."
    details = {"Location": s.office_name, "Result": "E-mail alerts work", "Sent": now_str()}
    card = _html_card(s, "✅ Test e-mail", "#047857", "TEST", "#d1fae5", "#065f46", details,
                      {"Download": "-", "Upload": "-", "Ping": "-"})
    return subject, text, card


def build_message(s: Settings, data: Dict[str, Any], now: Optional[datetime] = None) -> EmailMessage:
    if not s.recipients:
        raise ValueError("No e-mail recipients configured.")
    now = now or datetime.now()
    text, html_body = data["text"], data["html"]
    created = parse_ts(data.get("created"))
    if created and (now - created).total_seconds() > 120:
        note = (
            f"Delayed e-mail: this happened at {data['created']} but could only be sent now "
            f"({now.strftime(TS_FMT)}), for example because the internet was down."
        )
        text = note + "\n\n" + text
        box = (
            '<div style="max-width:540px; margin:0 auto 10px; padding:8px 12px; background:#fef9c3; '
            f'border:1px solid #fde68a; border-radius:6px; font-size:12px; color:#713f12;">{html.escape(note)}</div>'
        )
        html_body = html_body.replace("<!--DELAY-->", box)
    msg = EmailMessage()
    msg["From"] = s.email_user
    msg["To"] = ", ".join(s.alert_to)
    if s.alert_cc:
        msg["Cc"] = ", ".join(s.alert_cc)
    msg["Subject"] = data["subject"]
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    return msg


def _smtp_login(s: Settings):
    context = ssl.create_default_context()
    if s.smtp_encryption == "ssl":
        server = smtplib.SMTP_SSL(s.smtp_server, s.smtp_port, timeout=s.smtp_timeout, context=context)
    else:
        server = smtplib.SMTP(s.smtp_server, s.smtp_port, timeout=s.smtp_timeout)
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
    server.login(s.email_user, s.email_password)
    return server


def smtp_send(s: Settings, msg: EmailMessage) -> None:
    server = _smtp_login(s)
    try:
        server.send_message(msg, to_addrs=s.recipients)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def is_permanent_email_error(exc: BaseException) -> bool:
    """Errors that retrying will not fix (wrong password, bad address...)."""
    return isinstance(
        exc,
        (
            smtplib.SMTPAuthenticationError,
            smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused,
            smtplib.SMTPNotSupportedError,
            ssl.SSLCertVerificationError,
            ValueError,
        ),
    )


def email_error_hint(exc: BaseException) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "Login refused. Gmail/Outlook need an App Password, not your normal password."
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "The mail server's certificate was not trusted."
    if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused)):
        return "The server refused an address. Check EMAIL_USERNAME and ALERT_TO."
    return ""


class Notifier(threading.Thread):
    """Takes e-mails from a saved queue and sends them one by one.
    If sending fails (no internet, wrong password...) the e-mail stays in the
    queue and is retried with growing pauses. The monitor never waits for it."""

    def __init__(
        self,
        settings: Settings,
        queue: PersistentQueue,
        stop_event: threading.Event,
        sender: Callable[[Settings, EmailMessage], None] = smtp_send,
    ) -> None:
        super().__init__(name="notifier", daemon=True)
        self.s, self.queue, self.stop_event, self.sender = settings, queue, stop_event, sender
        self._next_try = 0.0
        self._failures = 0

    def enqueue(self, key: str, subject: str, text: str, html_body: str) -> None:
        if not self.s.email_enabled:
            logging.info("E-mail is disabled; not sending: %s", subject)
            return
        self.queue.put(
            {"key": key, "subject": subject, "text": text, "html": html_body, "created": now_str()}
        )
        logging.info("E-mail queued: %s", subject)

    def drop_pending(self, key_prefix: str) -> None:
        removed = self.queue.remove_where(
            lambda item: str(item["data"].get("key", "")).startswith(key_prefix)
        )
        if removed:
            logging.info("Dropped %d unsent e-mail(s) that the recovery e-mail replaces.", removed)

    def process_once(self, now: Optional[float] = None) -> bool:
        """Try to send the oldest waiting e-mail. True = something was done."""
        now = time.time() if now is None else now
        items = self.queue.peek(1)
        if not items or now < self._next_try:
            return False
        item = items[0]
        data = item["data"]
        created = parse_ts(data.get("created"))
        if created and (datetime.now() - created).total_seconds() > self.s.email_max_age_hours * 3600:
            self.queue.remove_ids([item["id"]])
            logging.warning("Dropped an e-mail older than %sh: %s", self.s.email_max_age_hours, data.get("subject"))
            return True
        try:
            self.sender(self.s, build_message(self.s, data))
        except Exception as exc:
            self._failures += 1
            permanent = is_permanent_email_error(exc)
            delay = min(30 * (2 ** (self._failures - 1)), 3600 if permanent else 900)
            self._next_try = now + delay
            logging.error(
                "E-mail NOT sent (%s). It is kept and retried in %ds. %s",
                short(exc, 120), delay, email_error_hint(exc),
            )
            return False
        self.queue.remove_ids([item["id"]])
        self._failures, self._next_try = 0, 0.0
        logging.info("E-mail sent: %s", data.get("subject"))
        return True

    def run(self) -> None:
        while not self.stop_event.is_set():
            progressed = False
            try:
                progressed = self.process_once()
            except Exception:
                logging.exception("Notifier error (will keep going)")
            self.stop_event.wait(1 if progressed else 5)


# =============================================================================
# 11. (reserved)  -- see section 12 for the decision logic
# =============================================================================


# =============================================================================
# 12. ALERT ENGINE: when do we e-mail, and when do we stay quiet?
# =============================================================================
class AlertEngine:
    """Decides what to do after each check.

        Ideal ........ nothing. If a CONFIRMED problem was open -> "recovered" e-mail.
        Low / Down ... count the bad checks in a row.
                       Reached CONFIRM_CHECKS (default 2) -> alert e-mail.
        Test Failed .. the speed test itself broke; we learn nothing, change nothing.

    So a single slow test or a 1-minute router hiccup never wakes anyone up,
    but a real problem is reported about 2 minutes after it starts."""

    def __init__(self, settings: Settings, state: Dict[str, Any], notifier: Notifier) -> None:
        self.s, self.state, self.notifier = settings, state, notifier

    @property
    def problem_active(self) -> bool:
        return self.state.get("problem") is not None

    def on_check(self, row: Dict[str, Any]) -> None:
        status = row["network_status"]
        problem = self.state.get("problem")

        if status == TEST_FAILED:
            return

        if status in (LOW, DOWN):
            if problem is None:
                problem = {
                    "type": status, "start": row["timestamp"], "bad_count": 1,
                    "confirmed": False, "alerted_type": None,
                }
                self.state["problem"] = problem
                logging.warning("Possible problem: %s. Re-checking to confirm.", status)
            else:
                problem["bad_count"] += 1
                if problem["type"] != status:
                    problem["type"] = status
            if not problem["confirmed"] and problem["bad_count"] >= self.s.confirm_checks:
                problem["confirmed"] = True
                logging.warning("Problem CONFIRMED: %s (since %s)", status, problem["start"])
            if problem["confirmed"] and problem.get("alerted_type") != problem["type"]:
                subject, text, card = build_alert_email(self.s, problem, row)
                self.notifier.enqueue(f"alert:{problem['start']}:{problem['type']}", subject, text, card)
                problem["alerted_type"] = problem["type"]
            return

        # status == IDEAL
        if problem is None:
            return
        if problem["confirmed"]:
            logging.info("Recovered from %s.", problem["type"])
            if self.s.send_recovery_email:
                # If the alert is still waiting to be sent (internet was down), the
                # recovery e-mail already contains everything, so drop the alert.
                self.notifier.drop_pending(f"alert:{problem['start']}")
                subject, text, card = build_recovery_email(self.s, problem, row)
                self.notifier.enqueue(f"recovery:{problem['start']}", subject, text, card)
        else:
            logging.info("The possible problem (%s) went away by itself. No e-mail sent.", problem["type"])
        self.state["problem"] = None


# =============================================================================
# 13. (reserved)
# =============================================================================


# =============================================================================
# 14. THE MONITOR: puts everything together
# =============================================================================
def gap_row(status: str, timestamp: str, note: str) -> Dict[str, Any]:
    row = {col: "" for col in CSV_COLUMNS}
    row.update(
        timestamp=timestamp, computer_status=status, ping_status=NOT_APPLICABLE,
        network_status=NOT_APPLICABLE, note=note,
    )
    return row


class Monitor:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.stop_event = threading.Event()
        self.state = load_state(settings.state_file)
        self.csv = CsvLog(settings)
        self.heartbeat = Heartbeat(settings, self.stop_event)
        self.email_queue = PersistentQueue(settings.email_outbox_file)
        self.notifier = Notifier(settings, self.email_queue, self.stop_event)
        self.upload_queue = PersistentQueue(settings.supabase_outbox_file)
        self.uploader: Optional[Uploader] = None
        if settings.supabase_enabled:
            client = SupabaseClient(settings.supabase_url, settings.supabase_key, settings.supabase_table)
            self.uploader = Uploader(settings, self.upload_queue, client, self.stop_event)
        self.engine = AlertEngine(settings, self.state, self.notifier)
        # replaceable pieces (tests replace these)
        self.ping_fn: Callable[[str, int], Dict[str, Any]] = run_ping
        self.internet_fn: Callable[[List[str]], Dict[str, Any]] = check_internet
        self.speedtest_fn: Callable[[Settings], Dict[str, Any]] = run_speedtest

    # -- recording ------------------------------------------------------------
    def record_row(self, row: Dict[str, Any]) -> None:
        """Save a row everywhere: CSV (always) and the Supabase queue."""
        if self.uploader:
            try:
                self.upload_queue.put(to_record(self.s, row))
                self.uploader.wake.set()
            except Exception:
                logging.exception("Could not queue the row for Supabase")
        self.csv.append(row)

    # -- start-up -------------------------------------------------------------
    def handle_startup_gap(self) -> None:
        gap = detect_gap(self.s, self.state)
        self.state["clean_stop_at"] = None
        if gap:
            duration = fmt_duration(gap["off_from"], gap["off_to"])
            if gap["kind"] == PC_OFF:
                note = f"PC was OFF (power cut or shutdown) from {gap['off_from']} to {gap['off_to']}, duration {duration}"
                logging.warning(note)
                self.record_row(gap_row(PC_OFF, gap["off_from"], note))
                subject, text, card = build_power_email(self.s, gap)
                self.notifier.enqueue(f"power:{gap['off_from']}", subject, text, card)
            else:
                note = f"Monitor was stopped on purpose from {gap['off_from']} to {gap['off_to']}, duration {duration}"
                logging.info(note)
                self.record_row(gap_row(PC_STOPPED, gap["off_from"], note))
        save_state(self.s.state_file, self.state)

    def wait_for_network(self) -> bool:
        """After a reboot the router needs a minute or two. Wait for it, so a
        power cut does not also look like an internet outage."""
        if self.s.network_wait <= 0:
            return True
        logging.info("Waiting up to %ds for the internet before the first check...", self.s.network_wait)
        deadline = time.time() + self.s.network_wait
        while not self.stop_event.is_set() and time.time() < deadline:
            if self.internet_fn(self.s.internet_targets)["ok"]:
                logging.info("The internet is reachable.")
                return True
            self.stop_event.wait(5)
        logging.warning("The internet is still not reachable. Starting the checks anyway.")
        return False

    # -- one check ------------------------------------------------------------
    def run_cycle(self, note: str = "") -> Dict[str, Any]:
        for off_from, off_to in self.heartbeat.pop_gaps():
            text = f"PC was asleep or frozen from {off_from} to {off_to}, duration {fmt_duration(off_from, off_to)}"
            logging.warning(text)
            self.record_row(gap_row(PC_ASLEEP, off_from, text))

        gateway = self.ping_fn(self.s.ping_target, self.s.ping_count)
        internet = self.internet_fn(self.s.internet_targets)
        gateway_ok = gateway["status"] == "Reachable"

        row: Dict[str, Any] = {col: "" for col in CSV_COLUMNS}
        row.update(
            timestamp=now_str(),
            computer_status=PC_ON,
            ping_status=gateway["status"],
            ping_avg_ms="" if gateway["avg_ms"] is None else f"{gateway['avg_ms']:.0f}",
            ping_loss_percent=f"{gateway['loss']:.0f}",
            network_status=DOWN,
            note=note,
        )

        def add_note(text: str) -> None:
            row["note"] = (row["note"] + " | " if row["note"] else "") + text

        if not internet["ok"]:
            add_note(
                "Router and internet both unreachable - LAN/router problem"
                if not gateway_ok
                else "Internet unreachable but the router answers - modem/ISP problem"
            )
        else:
            if not gateway_ok:
                add_note(f"Router {self.s.ping_target} did not answer ping, but the internet works")
            try:
                result = self.speedtest_fn(self.s)
                low = result["download"] < self.s.alert_threshold
                if self.s.alert_threshold_upload is not None and result["upload"] < self.s.alert_threshold_upload:
                    low = True
                    add_note(f"Upload below {self.s.alert_threshold_upload:.0f} Mbps")
                row.update(
                    download_mbps=f"{result['download']:.2f}",
                    upload_mbps=f"{result['upload']:.2f}",
                    idle_latency_ms="" if result["idle_latency"] is None else f"{result['idle_latency']:.2f}",
                    speedtest_packet_loss_percent=(
                        "" if result["packet_loss"] is None else f"{result['packet_loss']:.2f}"
                    ),
                    server=result["server"],
                    result_url=result["result_url"],
                    network_status=LOW if low else IDEAL,
                )
            except subprocess.TimeoutExpired:
                row["network_status"] = TEST_FAILED
                add_note(f"Speed test timed out after {self.s.speedtest_timeout}s")
            except Exception as exc:  # the test failed, but the internet answers ping
                row["network_status"] = TEST_FAILED
                add_note(f"Speed test failed: {short(exc, 120)}")

        self.record_row(row)
        logging.info(
            "PC=%s | Router=%s | Internet=%s | Down=%s Mbps | Up=%s Mbps | Status=%s",
            row["computer_status"], row["ping_status"], "OK" if internet["ok"] else "DOWN",
            row["download_mbps"] or "-", row["upload_mbps"] or "-", row["network_status"],
        )
        return row

    # -- main loop ------------------------------------------------------------
    def start(self) -> None:
        self.csv.ensure_header()
        self.handle_startup_gap()  # must run BEFORE the heartbeat is refreshed
        try:
            self.heartbeat.beat_once()
        except OSError as exc:
            logging.warning("Could not write the heartbeat file: %s", exc)
        for worker in (self.heartbeat, self.notifier, self.uploader):
            if worker:
                worker.start()

    def loop(self) -> None:
        note = "Monitor started"
        self.wait_for_network()
        while not self.stop_event.is_set():
            started = time.time()
            failed = False
            try:
                row = self.run_cycle(note)
                note = ""
                self.engine.on_check(row)
                save_state(self.s.state_file, self.state)
            except Exception:
                failed = True
                logging.exception("A check failed. The monitor keeps running.")
            interval = self.s.recheck_seconds if self.engine.problem_active else self.s.check_interval
            if failed:
                interval = min(interval, 60)
            wait = max(5.0, interval - (time.time() - started))
            logging.info("Next check in %.0f seconds...", wait)
            self.stop_event.wait(wait)

    def shutdown(self, graceful: bool = True) -> None:
        self.stop_event.set()
        if self.uploader:
            self.uploader.wake.set()
        for worker in (self.heartbeat, self.notifier, self.uploader):
            if worker and worker.is_alive():
                worker.join(timeout=5)
        if graceful:
            self.state["clean_stop_at"] = now_str()
        save_state(self.s.state_file, self.state)


# =============================================================================
# 15. SELF-CHECK:  python network_monitor.py --check
# =============================================================================
def run_check(settings: Settings, send_test: bool = False, run_speed: bool = False) -> int:
    problems = 0

    def report(ok: Optional[bool], text: str) -> None:
        nonlocal problems
        mark = "[ OK ]" if ok else ("[WARN]" if ok is None else "[FAIL]")
        if ok is False:
            problems += 1
        print(f"{mark} {text}")

    print(f"\nChecking the monitor for: {settings.office_name}\n" + "-" * 60)
    errors, warnings = settings.validate()
    for message in errors:
        report(False, f"Setting: {message}")
    for message in warnings:
        report(None, f"Setting: {message}")
    if not errors:
        report(True, "config.py settings look valid")

    gateway = run_ping(settings.ping_target, 2)
    if gateway["status"] == "Reachable":
        report(True, f"Router {settings.ping_target} answers ping ({_fmt(gateway['avg_ms'], ' ms')})")
    else:
        report(False, f"Router {settings.ping_target} does NOT answer ping - is PING_TARGET your router's address?")
    internet = check_internet(settings.internet_targets)
    if internet["ok"]:
        report(True, f"Internet reachable (via {internet['target']})")
    else:
        report(False, "Internet NOT reachable right now")

    try:
        report(True, f"Speedtest program found: {find_speedtest(settings)}")
        if run_speed:
            result = run_speedtest(settings)
            report(True, f"Speed test works: {result['download']:.1f} Mbps down, {result['upload']:.1f} Mbps up")
    except Exception as exc:
        report(False, f"Speedtest: {exc}")

    if settings.email_enabled and not [e for e in errors if "SMTP" in e or "EMAIL" in e or "ALERT_TO" in e]:
        try:
            server = _smtp_login(settings)
            server.quit()
            report(True, f"E-mail login works ({settings.smtp_server} as {settings.email_user})")
            if send_test:
                subject, text, card = build_test_email(settings)
                msg = build_message(settings, {"subject": subject, "text": text, "html": card, "created": now_str()})
                smtp_send(settings, msg)
                report(True, f"Test e-mail sent to {', '.join(settings.recipients)}")
            else:
                print("       (add --send-test-email to send a real test message)")
        except Exception as exc:
            report(False, f"E-mail: {short(exc, 140)} {email_error_hint(exc)}")
    elif not settings.email_enabled:
        report(None, "E-mail alerts are switched off (EMAIL_ENABLED = False)")

    if settings.supabase_enabled and not [e for e in errors if "SUPABASE" in e]:
        client = SupabaseClient(settings.supabase_url, settings.supabase_key, settings.supabase_table)
        result = client.sample()
        kind = supabase_key_kind(settings.supabase_key)
        if result.ok:
            report(True, f"Supabase table '{settings.supabase_table}' reachable (key type: {kind})")
        elif result.code in (401, 403):
            report(False, f"Supabase refused the key (HTTP {result.code}). Check SUPABASE_KEY.")
        elif result.code == 404:
            report(False, f"Supabase table '{settings.supabase_table}' not found. Run supabase_setup.sql first.")
        else:
            report(False, f"Supabase: HTTP {result.code} {short(result.body, 120)}")
    elif not settings.supabase_enabled:
        report(None, "Supabase is switched off (no URL/key) - the online dashboard will stay empty")

    print("-" * 60)
    print("Everything is fine." if problems == 0 else f"{problems} problem(s) found - fix them and run --check again.")
    return EXIT_OK if problems == 0 else EXIT_BAD_CONFIG


def backfill(settings: Settings) -> int:
    """Re-send the whole CSV to Supabase (duplicates are ignored)."""
    if not settings.supabase_enabled:
        print("Supabase is not configured in config.py.")
        return EXIT_BAD_CONFIG
    lock = SingleInstance(settings.lock_file)
    if not lock.acquire():
        print("The monitor is running. Stop it first (close its window), then run --backfill again.")
        return EXIT_ALREADY_RUNNING
    try:
        with open(settings.csv_file, "r", newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        queue = PersistentQueue(settings.supabase_outbox_file)
        added = 0
        for row in rows:
            if parse_ts(row.get("timestamp")):
                queue.put(to_record(settings, row))
                added += 1
        print(f"Queued {added} rows from the CSV. Uploading...")
        client = SupabaseClient(settings.supabase_url, settings.supabase_key, settings.supabase_table)
        outcome = flush_once(queue, client, settings.supabase_rejected_file)
        print(f"Uploaded {outcome.sent} rows." + (f" {outcome.rejected} rejected." if outcome.rejected else ""))
        if outcome.problem:
            print(f"Stopped early ({outcome.problem}): {outcome.detail}. Run --backfill again later.")
            return EXIT_CRASH
        return EXIT_OK
    finally:
        lock.release()


# =============================================================================
# 16. START-UP
# =============================================================================
def load_config_module() -> Optional[Any]:
    sys.path.insert(0, BASE_DIR)
    try:
        import config  # type: ignore

        return config
    except ModuleNotFoundError as exc:
        if exc.name != "config":
            raise
        print("config.py was not found next to network_monitor.py.")
        print("Copy config.example.py to config.py, edit it, then try again.")
    except Exception as exc:  # a typo inside config.py
        print(f"config.py has a mistake and cannot be read: {exc}")
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Network + Power monitor")
    parser.add_argument("--check", action="store_true", help="test all settings, then exit")
    parser.add_argument("--send-test-email", action="store_true", help="with --check: send a real test e-mail")
    parser.add_argument("--test-speedtest", action="store_true", help="with --check: run one real speed test")
    parser.add_argument("--backfill", action="store_true", help="re-send the whole CSV to Supabase")
    args = parser.parse_args(argv)

    cfg = load_config_module()
    if cfg is None:
        return EXIT_BAD_CONFIG
    settings = Settings(cfg)

    if args.check:
        return run_check(settings, args.send_test_email, args.test_speedtest)

    errors, warnings = settings.validate()
    if errors:
        print("The monitor cannot start because config.py needs fixing:")
        for message in errors:
            print(f"  - {message}")
        print("Run:  python network_monitor.py --check")
        return EXIT_BAD_CONFIG
    setup_logging(settings)
    configure_windows_os_resilience()
    for message in warnings:
        logging.warning("Setting: %s", message)

    if args.backfill:
        return backfill(settings)

    lock = SingleInstance(settings.lock_file)
    if not lock.acquire():
        print("The monitor is already running on this PC. Only one copy is allowed.")
        return EXIT_ALREADY_RUNNING

    monitor = Monitor(settings)

    def handle_stop(signum: int, frame: Any) -> None:
        logging.info("Stop requested. Shutting down...")
        monitor.stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), handle_stop)
            except (ValueError, OSError):
                pass

    graceful = False
    try:
        logging.info("=" * 70)
        logging.info("NETWORK + POWER MONITOR STARTED  (%s)", settings.office_name)
        logging.info(
            "Router=%s | Internet checks=%s | Alert below %.0f Mbps | Every %ds (re-check %ds, confirm %d)",
            settings.ping_target, ",".join(settings.internet_targets), settings.alert_threshold,
            settings.check_interval, settings.recheck_seconds, settings.confirm_checks,
        )
        logging.info(
            "Supabase: %s | E-mail: %s",
            f"table {settings.supabase_table}" if settings.supabase_enabled else "off",
            "on" if settings.email_enabled else "off",
        )
        logging.info("=" * 70)
        monitor.start()
        monitor.loop()
        graceful = True
        return EXIT_OK
    except KeyboardInterrupt:
        graceful = True
        return EXIT_OK
    except Exception:
        logging.exception("The monitor crashed")
        return EXIT_CRASH
    finally:
        monitor.shutdown(graceful=graceful)
        lock.release()
        if IS_WINDOWS:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass
        logging.info("Monitor stopped.")


if __name__ == "__main__":
    sys.exit(main())
