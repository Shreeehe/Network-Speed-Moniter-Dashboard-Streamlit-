# ⚡ Remote Network & Power Monitor (Supabase Edition)

A robust Windows network and power monitoring system with a remote, mobile-friendly Streamlit Cloud dashboard powered by **Supabase**.

---

## 🌟 Architecture

```
┌───────────────────────────────────────┐
│          Monitor PC (Local)           │
│  - network_monitor.py (every 5 mins)  │
│  - Reconstructs power cuts (OFF rows) │
│  - Ping tests + Ookla speedtest       │
│  - Appends to local CSV               │
│  - Uploads row to Supabase            │
│    (using secret/service_role key)    │
│  - Local outbox queue if offline      │
└───────────────────┬───────────────────┘
                    │
                    │ PostgREST REST API
                    ▼
┌───────────────────────────────────────┐
│               Supabase                │
│  - Table: `public.network_log`        │
│  - Row Level Security (RLS) ON        │
│  - Anon key has read-only access      │
└───────────────────┬───────────────────┘
                    │
                    │ PostgREST REST API (Read-only)
                    ▼
┌───────────────────────────────────────┐
│      Streamlit Community Cloud        │
│  - `dashboard/app.py`                 │
│  - Live status & 12m stale alert      │
│  - Bandwidth & ping charts (Plotly)   │
│  - Outage & power cut logs            │
│  - Password-protected (`APP_PASSWORD`)│
└───────────────────────────────────────┘
```

---

## 🚀 Setup Guide

### 1. Set Up the Supabase Database Tables
1. Log in to [Supabase](https://supabase.com) and open your project (or create a new free project).
2. Go to **SQL Editor** in the left sidebar.
3. Click **New query**, paste the entire contents of [supabase_setup.sql](supabase_setup.sql), and click **Run**.
4. This creates:
   - Table `public.network_log` (for New Office).
   - Table `public.network_log_old_office` (for Old Office).
   - Unique constraints `(ts, computer_status)` ensuring duplicate rows are never inserted.
   - Descending indexes on `ts` for fast time-series queries.
   - **Row Level Security (RLS)** with read and insert policies for both tables.

---

### 2. Locate Your Supabase Credentials
In your Supabase project dashboard:
1. Navigate to **Project Settings** (gear icon) -> **API**.
2. Note down:
   - **Project URL**: e.g., `https://abcdefghijkl.supabase.co`
   - **Project API Keys**:
     - `anon` / `public`: **Anon (publishable) key** -> Used by Streamlit Cloud (Read-only).
     - `service_role` / `secret`: **Service role key** -> Used ONLY by the local monitor machine in `config.py` (Write access).

---

### 3. Configure the Monitor Machine (`config.py`)
1. Open `config.py` on the monitor machine and fill in the Supabase section:
   ```python
   # ==============================
   # SUPABASE (cloud copy for Streamlit dashboard)
   # ==============================
   SUPABASE_URL = "https://your-project-id.supabase.co"
   SUPABASE_KEY = "your-supabase-service-role-secret-key"   # Secret key with write access

   # Choose the table based on location:
   # - For New Office PC: "network_log"
   # - For Old Office PC: "network_log_old_office"
   SUPABASE_TABLE = "network_log"

   SUPABASE_TZ_OFFSET = "+05:30"                            # Asia/Kolkata timezone offset
   ```
2. **Backfill Existing Data (One-Time)**:
   Upload your historical CSV data to Supabase by running:
   ```bash
   python network_monitor.py --backfill
   ```
   This safely reads `logs/network_speed_log.csv` and uploads all previous records to Supabase. Duplicate rows are automatically ignored.

> 🏢 **Multi-Office Support**: Both offices use the exact same monitor script and codebase. The only difference is `SUPABASE_TABLE` in `config.py`. The Streamlit dashboard includes an **Office Location** toggle at the top to switch between "🏢 New Office" and "🏚️ Old Office" with independent metrics and charts.

---

### 4. Deploy Dashboard to Streamlit Community Cloud
1. Push this project repository to GitHub (e.g. `Network-Speed-Moniter-Dashboard-Streamlit-`):
   > **Security Note**: `.gitignore` already protects `config.py`, `logs/`, state files, and secrets from being committed. Only public code and templates will be pushed.
2. Go to [Streamlit Community Cloud](https://share.streamlit.io/) and click **Create app**.
3. Select your repository: `Network-Speed-Moniter-Dashboard-Streamlit-`.
4. Main file path: `streamlit_app.py` (or `dashboard/app.py`).
5. Under **App settings -> Secrets**, paste your configuration:
   ```toml
   # Supabase Credentials (Read-only Anon Key)
   SUPABASE_URL = "https://your-project-id.supabase.co"
   SUPABASE_ANON_KEY = "your-supabase-anon-key"

   # Dashboard Password Protection
   APP_PASSWORD = "your_chosen_dashboard_password"
   ```
6. Click **Deploy!** Your dashboard is now live and accessible from anywhere.

---

### 5. Running the Dashboard Locally
To run the dashboard locally:
1. Create `.streamlit/secrets.toml` with your real Supabase credentials:
   ```toml
   SUPABASE_URL = "https://your-project-id.supabase.co"
   SUPABASE_ANON_KEY = "your-supabase-anon-key"
   APP_PASSWORD = ""
   ```
2. Start the local Streamlit dashboard:
   ```bash
   streamlit run dashboard/app.py
   ```
The dashboard connects directly to your live Supabase table.

---

## 🔌 Windows Startup & Power Reliability

To ensure monitoring starts automatically when the PC boots (especially after a power cut):

### 1. BIOS AC Power Loss Setting (Auto Power-On)
1. Turn on the PC and press `Del`, `F2`, or `F12` to enter BIOS/UEFI setup.
2. Navigate to **Advanced**, **Power Management**, or **ACPI Configuration**.
3. Locate **Restore on AC Power Loss**, **AC Power Recovery**, or **After Power Failure**.
4. Set it to **Power On** (or **Last State**).
5. Save and exit (`F10`).
Now, whenever grid electricity is restored after an outage, the PC powers on automatically without needing someone to press the physical power button.

### 2. Task Scheduler Setup
`start_monitor.bat` delays 60 seconds for Windows/WiFi/Router services to stabilize, and logs execution to `logs/bat_launch.log` and `logs/bat_output.log`.

1. Press `Win + R`, type `taskschd.msc`, and press Enter.
2. In the right panel, click **Create Task...** (not Basic Task):
   - **General**:
     - Name: `Network Speed Monitor`
     - Select: **Run whether user is logged on or not**
     - Check: **Run with highest privileges**
     - Configure for: **Windows 10 / Windows 11**
   - **Triggers**:
     - Click **New...**
     - Begin the task: **At startup**
     - (Optional) Delay task for: **1 minute**
   - **Actions**:
     - Click **New...**
     - Action: **Start a program**
     - Program/script: Browse and select `start_monitor.bat` (e.g. `D:\Projects\Network Speed Moniter Dashboard (Streamlit)\start_monitor.bat`)
     - Start in (required): Set to the project folder `D:\Projects\Network Speed Moniter Dashboard (Streamlit)`
   - **Conditions**:
     - Uncheck: **Start the task only if the computer is on AC power** (allows it to run if on UPS)
   - **Settings**:
     - Check: **If the task fails, restart every: 5 minutes**
     - Uncheck: **Stop the task if it runs longer than**
3. Click **OK** and enter your Windows account password when prompted.

---

## 🔒 Security & Secrets Hygiene

- `config.py` contains your SMTP mailbox password and the Supabase secret key (`service_role`). It is ignored by `.gitignore` and **must never be committed to git**.
- `config.example.py` is provided for reference and version control.
- Streamlit Cloud uses only the **anon (publishable) key**, which is strictly limited to read-only `SELECT` queries by PostgreSQL Row Level Security (RLS).
- The dashboard URL is protected with a password gate configured via `APP_PASSWORD`.

---

## 💡 Supabase Free Tier Egress Optimization (< 5 GB / Month)

To guarantee you remain well within Supabase's 5 GB/month free egress allowance:
1. **Column Filtering**: Only chart-essential columns (`ts, computer_status, ping_status, ping_avg_ms, ping_loss_percent, download_mbps, upload_mbps, network_status, note`) are fetched.
2. **7-Day Default Range**: The dashboard defaults to the last 7 days (~2,000 rows / ~150 KB), avoiding excessive full-history queries unless explicitly requested.
3. **5-Minute Cache TTL**: Cached with `st.cache_data(ttl=300)` since checks only happen every 5 minutes.
4. **Incremental Appending**: After the initial range load, clicking refresh only queries rows newer than the cached latest `ts` (`ts=gt.<cached_latest_ts>`). That means subsequent refreshes download only ~200 bytes!
5. **No Polling Loops**: No automated background polling loops or forced reloads.
