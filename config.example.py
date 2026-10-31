import os

# Folder that contains this file. All paths below are built from it, so the
# monitor works no matter where Windows starts it from (Startup folder,
# Task Scheduler, etc.).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ==============================
# NETWORK SPEED MONITOR SETTINGS
# ==============================

EXPECTED_SPEED = 100.0
ALERT_THRESHOLD = 85.0
CHECK_INTERVAL_SECONDS = 300

# NOTE: not used by the current script (alerts go out on the first Low/Down).
CONSECUTIVE_LOW_TESTS = 3


# ==============================
# PING / POWER MONITORING
# ==============================

PING_TARGET = "192.168.51.1"
PING_COUNT = 4

# After a Low/Down alert, re-test once after this many seconds.
RECOVERY_RECHECK_SECONDS = 120

# Heartbeat: the time is written to a file this often. If the heartbeat is
# older than OFF_GAP_SECONDS when the script starts, the PC was OFF.
HEARTBEAT_SECONDS = 30
OFF_GAP_SECONDS = 120


# ==============================
# OFFICIAL OOKLA SPEEDTEST CLI
# ==============================

SPEEDTEST_EXE = os.path.join(BASE_DIR, "speedtest.exe")
SPEEDTEST_TIMEOUT_SECONDS = 120
# Run full bandwidth test every 15 minutes (900s) to keep within Ookla free tier quotas.
# Ping / power checks still run every CHECK_INTERVAL_SECONDS (300s).
SPEEDTEST_INTERVAL_SECONDS = 900


# ==============================
# SMTP EMAIL CONFIGURATION
# ==============================

SMTP_SERVER = "smtp.example.com"
SMTP_PORT = 587

# Port 587 normally uses "starttls". "ssl" uses direct SSL (port 465). "none" sends unencrypted.
SMTP_ENCRYPTION = "starttls"

EMAIL_USERNAME = "your_email@example.com"
EMAIL_PASSWORD = "your_email_password_here"

# Alert recipients
ALERT_TO = [
    "alert_recipient@example.com",
]

ALERT_CC = [
    # "cc_recipient@example.com",
]

# Office display name used in email alert subjects & cards (e.g. "New Office" or "Old Office")
OFFICE_NAME = "New Office"

# Optional: Streamlit Community Cloud URL shown at the bottom of alert emails
DASHBOARD_URL = "https://your-app-name.streamlit.app"


# ==============================
# EMAIL RETRIES
# ==============================

SMTP_TIMEOUT_SECONDS = 30
EMAIL_RETRY_SECONDS = 30
EMAIL_MAX_RETRIES = 3

SEND_RECOVERY_EMAIL = True


# ==============================
# LOG FILES (all absolute)
# ==============================

LOG_DIRECTORY = os.path.join(BASE_DIR, "logs")
CSV_LOG_FILE = os.path.join(LOG_DIRECTORY, "network_speed_log.csv")
APP_LOG_FILE = os.path.join(LOG_DIRECTORY, "network_monitor.log")
STATE_FILE = os.path.join(LOG_DIRECTORY, "monitor_state.json")
HEARTBEAT_FILE = os.path.join(LOG_DIRECTORY, "heartbeat.txt")


# ==============================
# SUPABASE (cloud copy for the Streamlit dashboard)
# ==============================
# Leave URL/KEY empty to disable. The local CSV is always written either way.
# IMPORTANT:
# - Use the secret *service_role* key here on the local monitor (Project Settings -> API).
# - Do NOT use the *anon* key here: anon has read-only permissions under RLS and will fail.
# - NEVER commit config.py with real keys to git.
SUPABASE_URL = "https://your-project-id.supabase.co"
SUPABASE_KEY = "your-supabase-service-role-secret-key"
# Table name depends on which office PC this script runs on:
# - For New Office: use "network_log"
# - For Old Office: use "network_log_old_office"
SUPABASE_TABLE = "network_log"
SUPABASE_TZ_OFFSET = "+05:30"   # timezone of the CSV timestamps (India)
SUPABASE_OUTBOX_FILE = os.path.join(LOG_DIRECTORY, "supabase_outbox.jsonl")
