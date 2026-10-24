"""
data_reader.py - Data Reader for Supabase REST API (PostgREST)

Reads real monitoring data directly from the Supabase public.network_log table.
Optimized for Supabase Free Tier egress limits (< 5 GB/month):
  - Selects only chart-essential columns by default.
  - Server-side date range filtering.
  - Incremental fetching: fetches only rows newer than cached max timestamp.
  - PostgREST pagination for large result sets.
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import requests

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")
REQUEST_TIMEOUT = 15
PAGE_SIZE = 1000

# Egress-optimized column list
CHART_COLUMNS = "ts,computer_status,ping_status,ping_avg_ms,ping_loss_percent,download_mbps,upload_mbps,network_status,note"

try:
    import config
except ImportError:
    config = None


def to_utc_iso(dt_or_str):
    """
    Format a datetime or ISO string to standard UTC ISO 'YYYY-MM-DDTHH:MM:SSZ'.
    Using 'Z' instead of '+00:00' or '+05:30' avoids HTTP query string '+' encoding issues in PostgREST.
    """
    if not dt_or_str:
        return None
    try:
        dt = pd.to_datetime(dt_or_str, utc=True)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return str(dt_or_str)


def get_secret(secrets_dict, key, default=""):
    if secrets_dict:
        if key in secrets_dict:
            return str(secrets_dict[key])
        if key == "SUPABASE_ANON_KEY" and "SUPABASE_KEY" in secrets_dict:
            return str(secrets_dict["SUPABASE_KEY"])
    val = os.environ.get(key)
    if val:
        return val
    if key == "SUPABASE_ANON_KEY" and os.environ.get("SUPABASE_KEY"):
        return os.environ.get("SUPABASE_KEY")
    if config:
        if key == "SUPABASE_ANON_KEY" and hasattr(config, "SUPABASE_KEY"):
            return str(getattr(config, "SUPABASE_KEY", default))
        return str(getattr(config, key, default))
    return default


def fetch_supabase_rows(supabase_url, anon_key, table_name="network_log", start_iso=None, end_iso=None, after_ts_iso=None, columns=CHART_COLUMNS):
    """
    Fetch real rows from Supabase REST API (PostgREST) with pagination and server-side filtering.
    Selects only requested columns to minimize egress bandwidth.
    """
    url = f"{supabase_url.rstrip('/')}/rest/v1/{table_name}"
    headers = {
        "apikey": anon_key,
        "Authorization": f"Bearer {anon_key}",
        "Accept": "application/json",
    }

    all_rows = []
    offset = 0

    utc_start = to_utc_iso(start_iso)
    utc_end = to_utc_iso(end_iso)
    utc_after = to_utc_iso(after_ts_iso)

    while True:
        params = [
            ("select", columns),
            ("order", "ts.asc"),
            ("limit", str(PAGE_SIZE)),
            ("offset", str(offset)),
        ]
        if utc_after:
            params.append(("ts", f"gt.{utc_after}"))
        elif utc_start:
            params.append(("ts", f"gte.{utc_start}"))

        if utc_end:
            params.append(("ts", f"lte.{utc_end}"))

        resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Supabase REST API error ({resp.status_code}): {resp.text[:250]}"
            )

        batch = resp.json()
        if not batch:
            break

        all_rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    return all_rows


def process_dataframe(df):
    """Clean, parse timestamps to Asia/Kolkata, and cast types."""
    if df.empty or "ts" not in df.columns:
        return df

    # Parse timestamps to Asia/Kolkata timezone
    df["datetime"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(KOLKATA_TZ)
    df = df.sort_values("datetime").reset_index(drop=True)
    df["local_time"] = df["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")

    # Numeric conversions
    numeric_cols = [
        "download_mbps",
        "upload_mbps",
        "ping_avg_ms",
        "ping_loss_percent",
        "idle_latency_ms",
        "speedtest_packet_loss_percent",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Clean string defaults
    for col, default_val in [
        ("computer_status", "ON"),
        ("ping_status", "Reachable"),
        ("network_status", "Ideal"),
        ("note", ""),
    ]:
        if col not in df.columns:
            df[col] = default_val
        else:
            df[col] = df[col].fillna(default_val)

    return df


def load_network_data(supabase_url=None, anon_key=None, table_name="network_log", secrets_dict=None, start_dt=None, end_dt=None, existing_df=None):
    """
    Main loader for real Supabase data supporting multiple offices/tables:
    table_name can be 'network_log' (New Office) or 'network_log_old_office' (Old Office).
    
    Returns: (latest_row, dataframe, source_label, error_msg)
    """
    if not supabase_url:
        supabase_url = get_secret(secrets_dict, "SUPABASE_URL", "").strip()
    if not anon_key:
        anon_key = get_secret(secrets_dict, "SUPABASE_ANON_KEY", "").strip()

    if not supabase_url or not anon_key:
        return None, pd.DataFrame(), "None", (
            "SUPABASE_URL and SUPABASE_ANON_KEY are not configured. "
            "Please configure them in .streamlit/secrets.toml or Streamlit Cloud Secrets."
        )

    start_iso = start_dt.isoformat() if start_dt else None
    end_iso = end_dt.isoformat() if end_dt else None

    # Check if we can do an incremental fetch
    after_ts_iso = None
    if existing_df is not None and not existing_df.empty and "ts" in existing_df.columns:
        first_dt = existing_df["datetime"].min()
        if start_dt is None or first_dt <= start_dt:
            after_ts_iso = existing_df["ts"].max()

    try:
        new_rows = fetch_supabase_rows(
            supabase_url=supabase_url,
            anon_key=anon_key,
            table_name=table_name,
            start_iso=start_iso,
            end_iso=end_iso,
            after_ts_iso=after_ts_iso,
            columns=CHART_COLUMNS,
        )
        project_id = supabase_url.split("//")[-1].split(".")[0]
        source_label = f"Supabase ({project_id}) - Table: {table_name}"
    except Exception as exc:
        logging.warning("Supabase fetch failed: %s", exc)
        return None, (existing_df if existing_df is not None else pd.DataFrame()), "Supabase", f"Supabase error: {exc}"

    if new_rows:
        new_df = pd.DataFrame(new_rows)
        if existing_df is not None and not existing_df.empty and after_ts_iso:
            combined_df = pd.concat([existing_df, new_df], ignore_index=True)
            combined_df = combined_df.drop_duplicates(subset=["ts", "computer_status"], keep="last")
            df = process_dataframe(combined_df)
        else:
            df = process_dataframe(new_df)
    else:
        df = existing_df if existing_df is not None else pd.DataFrame()

    if df.empty or "ts" not in df.columns:
        return None, df, source_label, ""

    # Filter to requested range in memory to ensure bounds
    if start_dt:
        df = df[df["datetime"] >= start_dt]
    if end_dt:
        df = df[df["datetime"] <= end_dt]

    df = df.reset_index(drop=True)
    latest_row = df.iloc[-1].to_dict() if not df.empty else None

    return latest_row, df, source_label, ""
