"""
Network + Power monitor (runs every 5 minutes by default)

Each cycle:
  1. Computer status      -> ON (and OFF periods reconstructed from a heartbeat file after a power cut)
  2. Ping 192.168.51.1    -> Reachable / Unreachable
  3. Ookla speedtest      -> only if ping is reachable
  4. Network status       -> Ideal / Low / Totally Down
  5. Append row to CSV
  6. Alert e-mail for Low / Totally Down (body built from the CSV)
  7. One recovery re-check after 2 minutes
  8. Recovery e-mail when status returns to Ideal (in the re-check or any later run)
  9. Sleep until the next cycle
"""
import csv
import json
import logging
import os
import re
import shutil
import smtplib
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage

import config

# ----------------------------------------------------------------------------
# Settings (optional ones fall back to defaults if not in config.py)
# ----------------------------------------------------------------------------
TS_FMT = "%Y-%m-%d %H:%M:%S"

PING_TARGET = getattr(config, "PING_TARGET", "192.168.51.1")
PING_COUNT = getattr(config, "PING_COUNT", 4)
RECHECK_SECONDS = getattr(config, "RECOVERY_RECHECK_SECONDS", 120)
HEARTBEAT_SECONDS = getattr(config, "HEARTBEAT_SECONDS", 30)
OFF_GAP_SECONDS = getattr(config, "OFF_GAP_SECONDS", 120)  # heartbeat older than this = PC was off
STATE_FILE = getattr(config, "STATE_FILE", os.path.join(config.LOG_DIRECTORY, "monitor_state.json"))
HEARTBEAT_FILE = getattr(config, "HEARTBEAT_FILE", os.path.join(config.LOG_DIRECTORY, "heartbeat.txt"))

# Supabase (optional: leave URL/KEY empty in config.py to disable)
SUPABASE_URL = getattr(config, "SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = getattr(config, "SUPABASE_KEY", "")
SUPABASE_TABLE = getattr(config, "SUPABASE_TABLE", "network_log")
SUPABASE_TZ_OFFSET = getattr(config, "SUPABASE_TZ_OFFSET", "+05:30")  # CSV times are local (IST)
SUPABASE_OUTBOX_FILE = getattr(
    config, "SUPABASE_OUTBOX_FILE", os.path.join(config.LOG_DIRECTORY, "supabase_outbox.jsonl")
)
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_KEY)

IDEAL, LOW, DOWN = "Ideal", "Low", "Totally Down"

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

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def now_str():
    return datetime.now().strftime(TS_FMT)


def fmt_duration(start_str, end_str):
    try:
        s = datetime.strptime(start_str, TS_FMT)
        e = datetime.strptime(end_str, TS_FMT)
        total = int((e - s).total_seconds())
    except (ValueError, TypeError):
        return "unknown"
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def setup_logging():
    os.makedirs(config.LOG_DIRECTORY, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            logging.FileHandler(config.APP_LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


# ----------------------------------------------------------------------------
# State (survives restarts, so a pending e-mail is not lost after a power cut)
# ----------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    state.setdefault("problem", None)       # {type, start, alert_sent, recheck_done}
    state.setdefault("power_notice", None)  # {off_from, off_to}
    return state


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except OSError as exc:
        logging.error("Could not save state file: %s", exc)


# ----------------------------------------------------------------------------
# 1. Computer status: heartbeat + power-off detection
# ----------------------------------------------------------------------------
_gap_lock = threading.Lock()
_pending_gaps = []  # (off_from, off_to) gaps seen while the script kept running (sleep/hibernate)


def heartbeat_loop():
    """Writes the current time every HEARTBEAT_SECONDS. The last value left
    behind tells us when the power went off. Also notices if the PC was
    suspended (sleep/hibernate) while the script stayed alive."""
    last_tick = time.time()
    while True:
        now = time.time()
        if now - last_tick > OFF_GAP_SECONDS:
            with _gap_lock:
                _pending_gaps.append((
                    datetime.fromtimestamp(last_tick).strftime(TS_FMT),
                    datetime.fromtimestamp(now).strftime(TS_FMT),
                ))
        last_tick = now
        try:
            with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
                f.write(now_str())
        except OSError:
            pass
        time.sleep(HEARTBEAT_SECONDS)


def detect_power_gap():
    """Return (off_from, off_to) if the PC was off since the last run, else None."""
    try:
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            last = datetime.strptime(f.read().strip(), TS_FMT)
    except (OSError, ValueError):
        return None  # first run ever, nothing to compare

    now = datetime.now()
    if (now - last).total_seconds() > OFF_GAP_SECONDS:
        return last.strftime(TS_FMT), now.strftime(TS_FMT)
    return None


def check_computer_status():
    """Step 1 of every cycle. Returns (status, note).
    If this code is running, the PC is ON. Any OFF/sleep gap seen since the
    last cycle is written to the CSV as an OFF row and mentioned in the note."""
    with _gap_lock:
        gaps = list(_pending_gaps)
        _pending_gaps.clear()

    note = ""
    for off_from, off_to in gaps:
        duration = fmt_duration(off_from, off_to)
        text = f"PC was OFF/asleep from {off_from} to {off_to}, duration {duration}"
        logging.warning(text)
        append_row({
            "timestamp": off_from,
            "computer_status": "OFF",
            "ping_status": "N/A",
            "network_status": DOWN,
            "download_mbps": "0.00",
            "upload_mbps": "0.00",
            "note": text,
        })
        note = (note + " | " if note else "") + text

    logging.info("Computer status: ON")
    return "ON", note


# ----------------------------------------------------------------------------
# 2. Ping
# ----------------------------------------------------------------------------
def run_ping():
    command = ["ping", "-n", str(PING_COUNT), "-w", "1500", PING_TARGET]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=PING_COUNT * 3 + 10,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"status": "Unreachable", "avg_ms": None, "loss": 100.0}

    output = completed.stdout or ""

    # Windows reports "Destination host unreachable" as a received reply,
    # so count real replies by their TTL= field instead.
    replies = len(re.findall(r"TTL=", output, re.IGNORECASE))
    loss = (PING_COUNT - replies) / PING_COUNT * 100

    avg = None
    m = re.search(r"Average\s*=\s*(\d+)\s*ms", output, re.IGNORECASE)
    if m:
        avg = float(m.group(1))

    return {
        "status": "Reachable" if replies > 0 else "Unreachable",
        "avg_ms": avg,
        "loss": loss,
    }


# ----------------------------------------------------------------------------
# 3. Speedtest (official Ookla CLI)
# ----------------------------------------------------------------------------
def find_ookla_speedtest():
    candidates = []
    if config.SPEEDTEST_EXE:
        candidates.append(config.SPEEDTEST_EXE)
    candidates += [
        "speedtest.exe",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "speedtest.exe"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    found = shutil.which("speedtest.exe")
    if found and "\\scripts\\" not in os.path.normcase(found):
        return found

    raise FileNotFoundError("Official Ookla speedtest.exe was not found.")


def parse_number(pattern, text, name):
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        raise RuntimeError(f"Could not find {name} in Ookla output.")
    return float(match.group(1))


def run_ookla_speedtest():
    exe = find_ookla_speedtest()
    logging.info("Running official Ookla Speedtest...")

    completed = subprocess.run(
        [exe, "--accept-license", "--accept-gdpr"],
        capture_output=True,
        text=True,
        timeout=config.SPEEDTEST_TIMEOUT_SECONDS,
        creationflags=NO_WINDOW,
    )
    output = (completed.stdout or "") + "\n" + (completed.stderr or "")

    if completed.returncode != 0:
        first_line = next((ln.strip() for ln in output.splitlines() if ln.strip()), "no output")
        raise RuntimeError(f"Speedtest failed (exit {completed.returncode}): {first_line[:150]}")

    download = parse_number(r"Download:\s*([0-9]+(?:\.[0-9]+)?)\s*Mbps", output, "download")
    upload = parse_number(r"Upload:\s*([0-9]+(?:\.[0-9]+)?)\s*Mbps", output, "upload")
    idle_latency = parse_number(r"Idle Latency:\s*([0-9]+(?:\.[0-9]+)?)\s*ms", output, "idle latency")

    pl = re.search(r"Packet Loss:\s*([0-9]+(?:\.[0-9]+)?)\s*%", output, re.IGNORECASE)
    srv = re.search(r"Server:\s*(.+?)(?:\s+\(id:\s*\d+\))?\s*$", output, re.IGNORECASE | re.MULTILINE)
    url = re.search(r"Result URL:\s*(https?://\S+)", output, re.IGNORECASE)

    return {
        "download": download,
        "upload": upload,
        "idle_latency": idle_latency,
        "packet_loss": float(pl.group(1)) if pl else None,
        "server": srv.group(1).strip() if srv else "",
        "result_url": url.group(1).strip() if url else "",
    }


# ----------------------------------------------------------------------------
# 5. CSV
# ----------------------------------------------------------------------------
def ensure_csv_header():
    if os.path.exists(config.CSV_LOG_FILE):
        with open(config.CSV_LOG_FILE, "r", newline="", encoding="utf-8") as f:
            first = next(csv.reader(f), [])
        if first == CSV_COLUMNS:
            return
        # Old format from the previous script: keep it, start a fresh file.
        backup = config.CSV_LOG_FILE + "." + datetime.now().strftime("%Y%m%d_%H%M%S") + ".old"
        os.rename(config.CSV_LOG_FILE, backup)
        logging.warning("Old CSV layout moved to %s", backup)

    with open(config.CSV_LOG_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(CSV_COLUMNS)


def append_row(row):
    ensure_csv_header()
    supabase_enqueue(row)  # queued first, so it is kept even if the CSV is locked
    values = [row.get(col, "") for col in CSV_COLUMNS]
    for attempt in range(3):
        try:
            with open(config.CSV_LOG_FILE, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(values)
            return
        except PermissionError:
            # Usually the CSV is open in Excel.
            logging.warning("CSV is locked (open in Excel?). Retrying...")
            time.sleep(2)
    logging.error("Could not write to CSV: %s", row)


def read_rows_since(start_ts, limit=15):
    try:
        with open(config.CSV_LOG_FILE, "r", newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("timestamp", "") >= start_ts]
    except OSError:
        return []
    return rows[-limit:]


def format_rows(rows):
    lines = []
    for r in rows:
        line = (
            f'{r["timestamp"]} | PC {r["computer_status"]} | Ping {r["ping_status"]} '
            f'({r["ping_avg_ms"] or "-"} ms) | DL {r["download_mbps"]} / UL {r["upload_mbps"]} Mbps '
            f'| {r["network_status"]}'
        )
        if r.get("note"):
            line += f' | {r["note"]}'
        lines.append(line)
    return "\n".join(lines) if lines else "(no rows found in CSV)"


# ----------------------------------------------------------------------------
# Supabase sync (local CSV stays the master copy)
# Rows are queued in a local outbox file and sent in batches. If the internet is
# down (or the PC loses power) the outbox simply waits and is sent later.
# ----------------------------------------------------------------------------
def _num(value):
    try:
        return float(value) if value not in ("", None) else None
    except (TypeError, ValueError):
        return None


def supabase_enqueue(row):
    if not SUPABASE_ENABLED:
        return
    try:
        record = {
            "ts": row["timestamp"].replace(" ", "T") + SUPABASE_TZ_OFFSET,
            "computer_status": row.get("computer_status", ""),
            "ping_status": row.get("ping_status", ""),
            "ping_avg_ms": _num(row.get("ping_avg_ms")),
            "ping_loss_percent": _num(row.get("ping_loss_percent")),
            "download_mbps": _num(row.get("download_mbps")),
            "upload_mbps": _num(row.get("upload_mbps")),
            "idle_latency_ms": _num(row.get("idle_latency_ms")),
            "speedtest_packet_loss_percent": _num(row.get("speedtest_packet_loss_percent")),
            "server": row.get("server", ""),
            "network_status": row.get("network_status", ""),
            "note": row.get("note", ""),
            "result_url": row.get("result_url", ""),
        }
        with open(SUPABASE_OUTBOX_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        logging.error("Could not queue row for Supabase: %s", exc)


def _supabase_post(batch):
    url = f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}?on_conflict=ts,computer_status"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        # ignore rows that already exist, so re-sending is harmless
        "Prefer": "resolution=ignore-duplicates,return=minimal",
    }
    req = urllib.request.Request(
        url, data=json.dumps(batch).encode("utf-8"), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def supabase_flush():
    """Send everything in the outbox. Safe to call every cycle.
    Network / auth / server / paused-project problems keep the whole outbox for
    the next cycle. Only a row Supabase calls invalid (HTTP 400/409/422) is set
    aside in supabase_rejected.jsonl, so one bad row can never block the queue."""
    if not SUPABASE_ENABLED or not os.path.exists(SUPABASE_OUTBOX_FILE):
        return
    try:
        with open(SUPABASE_OUTBOX_FILE, "r", encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        if not records:
            os.remove(SUPABASE_OUTBOX_FILE)
            return

        rejected = []
        for i in range(0, len(records), 500):
            batch = records[i:i + 500]
            try:
                _supabase_post(batch)
            except urllib.error.HTTPError as exc:
                if exc.code not in (400, 409, 422):
                    raise  # auth, rate limit, paused project, server error: retry later
                for rec in batch:  # find the bad row(s) by sending one by one
                    try:
                        _supabase_post([rec])
                    except urllib.error.HTTPError as exc2:
                        if exc2.code not in (400, 409, 422):
                            raise
                        rejected.append(rec)

        if rejected:
            rejected_file = SUPABASE_OUTBOX_FILE.replace("outbox", "rejected")
            with open(rejected_file, "a", encoding="utf-8") as f:
                for rec in rejected:
                    f.write(json.dumps(rec) + "\n")
            logging.error("Supabase refused %s row(s); saved in %s (they are still in the CSV).",
                          len(rejected), rejected_file)

        os.remove(SUPABASE_OUTBOX_FILE)
        logging.info("Supabase: sent %s row(s).", len(records) - len(rejected))

    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        logging.error("Supabase upload failed (HTTP %s), will retry next cycle: %s", exc.code, body)
        if "row-level security" in body.lower() or "42501" in body:
            logging.error(
                "HINT: Row Level Security (RLS) blocked the upload. Ensure SUPABASE_KEY in config.py "
                "is your secret 'service_role' key, NOT the read-only 'anon' key."
            )
    except Exception as exc:
        logging.warning("Supabase upload failed, will retry next cycle: %s", exc)


def supabase_backfill_from_csv():
    """One-time: python network_monitor.py --backfill"""
    if not SUPABASE_ENABLED:
        print("Set SUPABASE_URL and SUPABASE_KEY in config.py first.")
        return
    with open(config.CSV_LOG_FILE, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        supabase_enqueue(r)
    print(f"Queued {len(rows)} rows from the CSV.")
    supabase_flush()
    print("Done." if not os.path.exists(SUPABASE_OUTBOX_FILE) else "Upload failed, see the log.")


# ----------------------------------------------------------------------------
# 2-4. One check cycle -> one CSV row
# ----------------------------------------------------------------------------
def run_cycle(note=""):
    # Step 1: computer status (before any network test)
    computer_status, computer_note = check_computer_status()
    if computer_note:
        note = (note + " | " if note else "") + computer_note

    # Step 2: ping
    ping = run_ping()
    row = {
        "timestamp": now_str(),
        "computer_status": computer_status,
        "ping_status": ping["status"],
        "ping_avg_ms": "" if ping["avg_ms"] is None else f'{ping["avg_ms"]:.0f}',
        "ping_loss_percent": f'{ping["loss"]:.0f}',
        "download_mbps": "0.00",
        "upload_mbps": "0.00",
        "idle_latency_ms": "",
        "speedtest_packet_loss_percent": "",
        "server": "",
        "network_status": DOWN,
        "note": note,
        "result_url": "",
    }

    def add_note(text):
        row["note"] = (row["note"] + " | " if row["note"] else "") + text

    if ping["status"] != "Reachable":
        add_note(f"Ping to {PING_TARGET} failed")
    else:
        try:
            st = run_ookla_speedtest()
            row.update({
                "download_mbps": f'{st["download"]:.2f}',
                "upload_mbps": f'{st["upload"]:.2f}',
                "idle_latency_ms": f'{st["idle_latency"]:.2f}',
                "speedtest_packet_loss_percent": "" if st["packet_loss"] is None else f'{st["packet_loss"]:.2f}',
                "server": st["server"],
                "result_url": st["result_url"],
                "network_status": LOW if st["download"] < config.ALERT_THRESHOLD else IDEAL,
            })
        except subprocess.TimeoutExpired:
            add_note(f"Speedtest timed out after {config.SPEEDTEST_TIMEOUT_SECONDS}s")
        except Exception as exc:
            add_note(str(exc))

    append_row(row)
    logging.info(
        "PC=%s | Ping=%s | DL=%s Mbps | UL=%s Mbps | Status=%s",
        row["computer_status"], row["ping_status"],
        row["download_mbps"], row["upload_mbps"], row["network_status"],
    )
    return row


# ----------------------------------------------------------------------------
# E-mail
# ----------------------------------------------------------------------------
def send_email(subject, body):
    recipients = [x.strip() for x in config.ALERT_TO if x.strip()]
    cc = [x.strip() for x in config.ALERT_CC if x.strip()]
    all_recipients = recipients + cc
    if not all_recipients:
        raise ValueError("No email recipients configured.")

    msg = EmailMessage()
    msg["From"] = config.EMAIL_USERNAME
    msg["To"] = ", ".join(recipients)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body)

    for attempt in range(1, config.EMAIL_MAX_RETRIES + 1):
        try:
            logging.info("Sending email (attempt %s/%s): %s", attempt, config.EMAIL_MAX_RETRIES, subject)
            enc = config.SMTP_ENCRYPTION.lower()
            if enc == "ssl":
                with smtplib.SMTP_SSL(config.SMTP_SERVER, config.SMTP_PORT,
                                      timeout=config.SMTP_TIMEOUT_SECONDS) as server:
                    server.login(config.EMAIL_USERNAME, config.EMAIL_PASSWORD)
                    server.send_message(msg, to_addrs=all_recipients)
            else:
                with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT,
                                  timeout=config.SMTP_TIMEOUT_SECONDS) as server:
                    server.ehlo()
                    if enc == "starttls":
                        server.starttls()
                        server.ehlo()
                    server.login(config.EMAIL_USERNAME, config.EMAIL_PASSWORD)
                    server.send_message(msg, to_addrs=all_recipients)
            logging.info("Email sent successfully.")
            return True
        except (OSError, smtplib.SMTPException, socket.error) as exc:
            logging.error("Email attempt failed: %s", exc)
            if attempt < config.EMAIL_MAX_RETRIES:
                time.sleep(config.EMAIL_RETRY_SECONDS)

    logging.error("All email attempts failed. Will retry in a later cycle.")
    return False


def alert_body(problem):
    rows = read_rows_since(problem["start"])
    return (
        f"Network alert: {problem['type'].upper()}\n\n"
        f"Problem started: {problem['start']}\n"
        f"Expected speed: {config.EXPECTED_SPEED:.2f} Mbps\n"
        f"Low-speed threshold: {config.ALERT_THRESHOLD:.2f} Mbps\n"
        f"Ping target: {PING_TARGET}\n\n"
        f"Recent log (from CSV):\n{format_rows(rows)}\n"
    )


def recovery_body(problem, row):
    rows = read_rows_since(problem["start"])
    extra = ""
    if not problem["alert_sent"]:
        extra = ("\nNote: the alert e-mail could not be sent during the problem "
                 "(no connectivity), so this is the first message.\n")
    return (
        f"Network recovered (was: {problem['type']})\n\n"
        f"Problem started: {problem['start']}\n"
        f"Recovered at:    {row['timestamp']}\n"
        f"Duration:        {fmt_duration(problem['start'], row['timestamp'])}\n"
        f"Current speed:   {row['download_mbps']} Mbps down / {row['upload_mbps']} Mbps up\n"
        f"{extra}\n"
        f"Log during the problem (from CSV):\n{format_rows(rows)}\n"
    )


def power_body(notice):
    rows = read_rows_since(notice["off_from"])
    return (
        "PC was OFF (power cut / shutdown)\n\n"
        f"Last seen ON: {notice['off_from']}\n"
        f"Back ON at:   {notice['off_to']}\n"
        f"Off duration: {fmt_duration(notice['off_from'], notice['off_to'])}\n\n"
        f"Log after the outage (from CSV):\n{format_rows(rows)}\n"
    )


# ----------------------------------------------------------------------------
# 6-8. Alert / recovery logic
# ----------------------------------------------------------------------------
def process(row, state):
    status = row["network_status"]
    can_email = row["ping_status"] == "Reachable"  # if the router is unreachable, mail cannot go out
    problem = state["problem"]

    # Pending power-outage notice (set at startup after a power cut)
    notice = state["power_notice"]
    if notice and can_email:
        subject = (f"POWER OFF: PC was off {notice['off_from']} to {notice['off_to']} "
                   f"({fmt_duration(notice['off_from'], notice['off_to'])})")
        if send_email(subject, power_body(notice)):
            state["power_notice"] = None

    if status in (LOW, DOWN):
        if problem is None or problem["type"] != status:
            problem = {
                "type": status,
                "start": problem["start"] if problem else row["timestamp"],
                "alert_sent": False,
                "recheck_done": False,
            }
            state["problem"] = problem
            logging.warning("Problem state: %s (since %s)", status, problem["start"])

        if not problem["alert_sent"] and can_email:
            subject = f"NETWORK ALERT: {status} ({row['download_mbps']} Mbps)"
            if send_email(subject, alert_body(problem)):
                problem["alert_sent"] = True

    elif problem is not None:  # status is Ideal and there was a problem
        logging.info("Recovered from %s.", problem["type"])
        if not getattr(config, "SEND_RECOVERY_EMAIL", True):
            state["problem"] = None
        else:
            subject = f"NETWORK RECOVERED from {problem['type']}: {row['download_mbps']} Mbps"
            if send_email(subject, recovery_body(problem, row)):
                state["problem"] = None
            # else: keep the problem record, the mail is retried next cycle


# ----------------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------------
def main():
    setup_logging()
    if "--backfill" in sys.argv:
        supabase_backfill_from_csv()
        return
    logging.info("=" * 70)
    logging.info("NETWORK + POWER MONITOR STARTED")
    logging.info(
        "Ping=%s | Threshold=%.2f Mbps | Interval=%ss | Recheck=%ss",
        PING_TARGET, config.ALERT_THRESHOLD, config.CHECK_INTERVAL_SECONDS, RECHECK_SECONDS,
    )
    logging.info("Target Supabase Table: %s (Supabase %s)", SUPABASE_TABLE, "Enabled" if SUPABASE_ENABLED else "Disabled")
    logging.info("=" * 70)

    state = load_state()
    ensure_csv_header()
    startup_note = "Monitor started"

    gap = detect_power_gap()
    if gap:
        off_from, off_to = gap
        duration = fmt_duration(off_from, off_to)
        logging.warning("PC was OFF from %s to %s (%s)", off_from, off_to, duration)
        append_row({
            "timestamp": off_from,
            "computer_status": "OFF",
            "ping_status": "N/A",
            "network_status": DOWN,
            "download_mbps": "0.00",
            "upload_mbps": "0.00",
            "note": f"PC OFF (power cut/shutdown) from {off_from} to {off_to}, duration {duration}",
        })
        state["power_notice"] = {"off_from": off_from, "off_to": off_to}
        startup_note = "First check after PC was OFF"
        save_state(state)

    threading.Thread(target=heartbeat_loop, daemon=True).start()

    while True:
        cycle_start = time.time()
        try:
            row = run_cycle(startup_note)
            startup_note = ""
            process(row, state)

            # 7. One recovery re-check after 2 minutes
            problem = state["problem"]
            if problem and row["network_status"] != IDEAL and not problem["recheck_done"]:
                problem["recheck_done"] = True
                save_state(state)
                logging.info("Re-checking in %s seconds...", RECHECK_SECONDS)
                time.sleep(RECHECK_SECONDS)
                row = run_cycle("2-min recovery re-check")
                process(row, state)

        except Exception as exc:
            logging.exception("Cycle failed: %s", exc)

        save_state(state)
        supabase_flush()

        wait = max(5, config.CHECK_INTERVAL_SECONDS - (time.time() - cycle_start))
        logging.info("Next cycle in %.0f seconds...", wait)
        time.sleep(wait)


if __name__ == "__main__":
    main()