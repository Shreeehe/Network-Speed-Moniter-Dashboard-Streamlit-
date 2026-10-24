"""
app.py - Streamlit Monitoring Dashboard for Network & Power Monitor
Reads from Supabase REST API (PostgREST) with strict egress optimization (< 5 GB/month)
and deployable to Streamlit Community Cloud.
"""
import math
import os
import sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Add dashboard folder to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_reader

KOLKATA_TZ = ZoneInfo("Asia/Kolkata")
TS_FMT = "%Y-%m-%d %H:%M:%S"

st.set_page_config(
    page_title="Network & Power Monitor",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ----------------------------------------------------------------------------
# Custom CSS for Premium Design & Responsive Layout
# ----------------------------------------------------------------------------
st.markdown(
    """
    <style>
    /* Metric Cards */
    .metric-card {
        background: linear-gradient(135deg, rgba(255, 255, 255, 0.05) 0%, rgba(255, 255, 255, 0.02) 100%);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 12px;
        padding: 16px 20px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
        margin-bottom: 12px;
    }
    .metric-title {
        font-size: 0.82rem;
        font-weight: 600;
        color: #94a3b8;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 4px;
    }
    .metric-value {
        font-size: 1.75rem;
        font-weight: 800;
        color: #f8fafc;
        line-height: 1.2;
    }
    .metric-sub {
        font-size: 0.78rem;
        color: #64748b;
        margin-top: 4px;
    }

    /* Live Status Banner Cards */
    .status-banner {
        border-radius: 14px;
        padding: 22px 28px;
        margin-bottom: 24px;
        display: flex;
        flex-direction: column;
        border: 1px solid rgba(255, 255, 255, 0.15);
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.15);
    }
    .status-banner-ideal {
        background: linear-gradient(135deg, #064e3b 0%, #065f46 60%, #047857 100%);
        color: #ecfdf5;
        border-color: #10b981;
    }
    .status-banner-low {
        background: linear-gradient(135deg, #78350f 0%, #92400e 60%, #b45309 100%);
        color: #fffbeb;
        border-color: #f59e0b;
    }
    .status-banner-down {
        background: linear-gradient(135deg, #7f1d1d 0%, #991b1b 60%, #b91c1c 100%);
        color: #fef2f2;
        border-color: #ef4444;
    }
    .status-banner-off {
        background: linear-gradient(135deg, #1e293b 0%, #334155 60%, #475569 100%);
        color: #f1f5f9;
        border-color: #64748b;
    }
    .status-badge {
        display: inline-block;
        padding: 4px 12px;
        border-radius: 9999px;
        font-size: 0.8rem;
        font-weight: 700;
        letter-spacing: 0.05em;
        text-transform: uppercase;
        background: rgba(255, 255, 255, 0.2);
    }

    /* Stale Alert Banner */
    .stale-alert {
        background: linear-gradient(135deg, #dc2626 0%, #991b1b 100%);
        border: 2px solid #f87171;
        border-radius: 12px;
        padding: 18px 22px;
        margin-bottom: 24px;
        color: #ffffff;
        font-weight: 500;
        box-shadow: 0 6px 20px rgba(220, 38, 38, 0.3);
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------------------
# 1. Password Gate
# ----------------------------------------------------------------------------
def check_password():
    app_pwd = None
    try:
        app_pwd = st.secrets.get("APP_PASSWORD")
    except Exception:
        app_pwd = os.environ.get("APP_PASSWORD")

    # If no password configured, permit access (for development / unconfigured environments)
    if not app_pwd:
        return True

    if st.session_state.get("authenticated", False):
        return True

    st.markdown("### 🔒 Network Monitor - Authentication")
    st.write("This dashboard is password-protected. Please enter the access password.")
    user_input = st.text_input("Password", type="password", key="password_input")
    if st.button("Log In", type="primary"):
        if user_input == app_pwd:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password. Please try again.")
    return False


if not check_password():
    st.stop()


# ----------------------------------------------------------------------------
# Cached Data Loader (TTL = 300s / 5 minutes to stay inside free tier egress)
# ----------------------------------------------------------------------------
@st.cache_data(ttl=300)
def cached_load_data(supabase_url, anon_key, table_name, start_iso, end_iso):
    """
    Cached data loader for Supabase REST API (PostgREST).
    Pure function with 5-minute TTL to stay well within free tier egress limits.
    """
    if not supabase_url or not anon_key:
        return None, pd.DataFrame(), "None", (
            "SUPABASE_URL and SUPABASE_ANON_KEY are not configured. "
            "Please configure them in .streamlit/secrets.toml or Streamlit Cloud Secrets."
        )

    start_dt = datetime.fromisoformat(start_iso) if start_iso else None
    end_dt = datetime.fromisoformat(end_iso) if end_iso else None

    latest_row, df, source_label, error_msg = data_reader.load_network_data(
        supabase_url=supabase_url,
        anon_key=anon_key,
        table_name=table_name,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    return latest_row, df, source_label, error_msg


# ----------------------------------------------------------------------------
# Helper Functions
# ----------------------------------------------------------------------------
def format_seconds_to_str(total_seconds):
    if total_seconds <= 0 or math.isnan(total_seconds):
        return "0s"
    total_seconds = int(total_seconds)
    hours, rem = divmod(total_seconds, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def compute_outage_episodes(df):
    """
    Groups consecutive non-Ideal or OFF rows into episodes.
    Returns: list of dicts: [ {type, start, end, duration_s, min_dl, notes} ]
    """
    if df is None or df.empty:
        return []

    episodes = []
    current_ep = None

    for _, row in df.iterrows():
        status = row.get("network_status", "Ideal")
        pc_status = row.get("computer_status", "ON")
        ts = row["datetime"]
        dl = row.get("download_mbps", 0.0)

        is_problem = (status != "Ideal") or (pc_status == "OFF")

        if is_problem:
            ep_type = "Power Cut (PC OFF)" if pc_status == "OFF" else status
            if current_ep is None:
                current_ep = {
                    "type": ep_type,
                    "start": ts,
                    "end": ts,
                    "min_dl": dl if not math.isnan(dl) else 0.0,
                    "notes": [row.get("note", "")] if row.get("note") else [],
                    "count": 1,
                }
            else:
                current_ep["end"] = ts
                current_ep["count"] += 1
                if not math.isnan(dl) and dl < current_ep["min_dl"]:
                    current_ep["min_dl"] = dl
                if row.get("note") and row["note"] not in current_ep["notes"]:
                    current_ep["notes"].append(row["note"])
                if ep_type == "Power Cut (PC OFF)":
                    current_ep["type"] = "Power Cut (PC OFF)"
        else:
            if current_ep is not None:
                duration_s = max(300, (current_ep["end"] - current_ep["start"]).total_seconds())
                current_ep["duration_s"] = duration_s
                current_ep["duration_str"] = format_seconds_to_str(duration_s)
                episodes.append(current_ep)
                current_ep = None

    if current_ep is not None:
        duration_s = max(300, (current_ep["end"] - current_ep["start"]).total_seconds())
        current_ep["duration_s"] = duration_s
        current_ep["duration_str"] = format_seconds_to_str(duration_s)
        episodes.append(current_ep)

    return episodes


# ----------------------------------------------------------------------------
# Header, Office Selector & Range Controls
# ----------------------------------------------------------------------------
col_head, col_office, col_action = st.columns([3, 2, 1])
with col_head:
    st.title("⚡ Network & Power Monitor")
with col_office:
    office_choice = st.radio(
        "Office Location",
        options=["🏢 New Office", "🏚️ Old Office"],
        index=0,
        horizontal=True,
    )
with col_action:
    st.write("")
    if st.button("🔄 Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

office_table_map = {
    "🏢 New Office": "network_log",
    "🏚️ Old Office": "network_log_old_office",
}
current_table = office_table_map.get(office_choice, "network_log")

now_kolkata = datetime.now(KOLKATA_TZ)
max_date = now_kolkata.date()

ctrl_c1, ctrl_c2 = st.columns([3, 2])
with ctrl_c1:
    range_option = st.radio(
        "Time Range",
        options=["Today", "7 days", "30 days", "Custom"],
        index=1,  # Default to last 7 days per egress optimization
        horizontal=True,
    )

selected_start_dt = None
selected_end_dt = None

if range_option == "Today":
    selected_start_dt = datetime.combine(max_date, datetime.min.time(), tzinfo=KOLKATA_TZ)
    selected_end_dt = datetime.combine(max_date, datetime.max.time(), tzinfo=KOLKATA_TZ)
elif range_option == "7 days":
    selected_start_dt = datetime.combine(max_date - timedelta(days=7), datetime.min.time(), tzinfo=KOLKATA_TZ)
    selected_end_dt = datetime.combine(max_date, datetime.max.time(), tzinfo=KOLKATA_TZ)
elif range_option == "30 days":
    selected_start_dt = datetime.combine(max_date - timedelta(days=30), datetime.min.time(), tzinfo=KOLKATA_TZ)
    selected_end_dt = datetime.combine(max_date, datetime.max.time(), tzinfo=KOLKATA_TZ)
elif range_option == "Custom":
    with ctrl_c2:
        d1, d2 = st.columns(2)
        with d1:
            start_date = st.date_input("Start Date", value=max_date - timedelta(days=7))
        with d2:
            end_date = st.date_input("End Date", value=max_date)
        if start_date > end_date:
            st.error("⚠️ Start Date cannot be after End Date.")
            st.stop()
        selected_start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=KOLKATA_TZ)
        selected_end_dt = datetime.combine(end_date, datetime.max.time(), tzinfo=KOLKATA_TZ)

# Resolve credentials cleanly from st.secrets or fallbacks
supabase_url = ""
anon_key = ""
try:
    supabase_url = str(st.secrets.get("SUPABASE_URL", "")).strip()
    anon_key = str(st.secrets.get("SUPABASE_ANON_KEY", st.secrets.get("SUPABASE_KEY", ""))).strip()
except Exception:
    pass

if not supabase_url or not anon_key:
    supabase_url = data_reader.get_secret({}, "SUPABASE_URL", "").strip()
    anon_key = data_reader.get_secret({}, "SUPABASE_ANON_KEY", "").strip()

# Fetch data with cached logic (5m TTL per table and range)
start_iso = selected_start_dt.isoformat() if selected_start_dt else None
end_iso = selected_end_dt.isoformat() if selected_end_dt else None

latest_row, df, source_label, error_msg = cached_load_data(
    supabase_url=supabase_url,
    anon_key=anon_key,
    table_name=current_table,
    start_iso=start_iso,
    end_iso=end_iso,
)

st.caption(f"Office: **{office_choice}** | Table: **{current_table}** | Source: **{source_label}** | Timezone: **Asia/Kolkata** (IST)")

if error_msg and (df is None or df.empty):
    if "PGRST205" in error_msg or "Could not find the table" in error_msg:
        st.warning(
            f"⚠️ Supabase table **`{current_table}`** for **{office_choice}** has not been created yet!\n\n"
            f"To create it, open your **Supabase Dashboard -> SQL Editor** and run the SQL query from `supabase_setup.sql`."
        )
    else:
        st.error(error_msg)
    st.stop()

if df is None or df.empty:
    st.info(
        f"No records found in Supabase table **{current_table}** for **{office_choice}** in this time range. "
        f"Ensure the monitor script on that office PC is running with `SUPABASE_TABLE = \"{current_table}\"`."
    )
    st.stop()

# ----------------------------------------------------------------------------
# 2. Live Status Banner & 12-minute Stale Outage Alert
# ----------------------------------------------------------------------------
if not latest_row:
    latest_row = df.iloc[-1].to_dict()

newest_ts_val = latest_row.get("ts")
newest_dt = None
if newest_ts_val:
    try:
        newest_dt = pd.to_datetime(newest_ts_val, utc=True).tz_convert(KOLKATA_TZ)
    except Exception:
        pass

elapsed_seconds = 0
is_stale = False
last_updated_str = "Unknown"

if newest_dt:
    elapsed_seconds = max(0, (now_kolkata - newest_dt).total_seconds())
    last_updated_str = newest_dt.strftime("%Y-%m-%d %H:%M:%S")
    # If now - newest ts > 12 minutes (720 seconds), display prominent red banner
    if elapsed_seconds > 720:
        is_stale = True

if is_stale:
    time_ago_str = format_seconds_to_str(elapsed_seconds)
    st.markdown(
        f"""
        <div class="stale-alert">
            <h3 style="margin: 0 0 6px 0; font-size: 1.3rem;">⚠️ Outage Warning: PC or Internet Offline</h3>
            <div style="font-size: 1.05rem;">
                No data since <strong>{last_updated_str}</strong> ({time_ago_str} ago): the PC, power or internet is likely down.
            </div>
            <div style="font-size: 0.9rem; margin-top: 6px; opacity: 0.95;">
                This is how you notice a power cut from outside, since the PC cannot send an alert while it has no power.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

current_net_status = latest_row.get("network_status", "Ideal")
computer_status = latest_row.get("computer_status", "ON")
ping_status = latest_row.get("ping_status", "Reachable")
ping_avg = latest_row.get("ping_avg_ms")
ping_avg_str = f"{ping_avg:.0f}" if ping_avg is not None and not math.isnan(ping_avg) else "-"
dl_speed = latest_row.get("download_mbps", 0.0)
ul_speed = latest_row.get("upload_mbps", 0.0)
dl_speed = 0.0 if (dl_speed is None or math.isnan(dl_speed)) else float(dl_speed)
ul_speed = 0.0 if (ul_speed is None or math.isnan(ul_speed)) else float(ul_speed)

# Status color map: Ideal = green, Low = amber, Totally Down = red, OFF = dark grey
if computer_status == "OFF":
    banner_class = "status-banner-off"
    status_icon = "⚫"
    status_display = "PC OFF (POWER CUT)"
elif current_net_status == "Low":
    banner_class = "status-banner-low"
    status_icon = "🟡"
    status_display = "LOW SPEED"
elif current_net_status == "Totally Down":
    banner_class = "status-banner-down"
    status_icon = "🔴"
    status_display = "TOTALLY DOWN"
else:
    banner_class = "status-banner-ideal"
    status_icon = "🟢"
    status_display = "IDEAL"

st.markdown(
    f"""
    <div class="status-banner {banner_class}">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 14px;">
            <div>
                <span class="status-badge">{office_choice} &bull; {computer_status} &bull; {ping_status}</span>
                <h1 style="margin: 8px 0 4px 0; font-size: 2.2rem; font-weight: 800; color: inherit;">
                    {status_icon} {status_display}
                </h1>
                <div style="font-size: 0.95rem; opacity: 0.9;">
                    Last updated: <strong>{last_updated_str}</strong> ({format_seconds_to_str(elapsed_seconds)} ago)
                </div>
            </div>
            <div style="text-align: right; min-width: 220px;">
                <div style="font-size: 1.6rem; font-weight: 700;">
                    ↓ {dl_speed:.2f} <span style="font-size: 1rem; font-weight: 400;">Mbps</span>
                    &nbsp;&nbsp;
                    ↑ {ul_speed:.2f} <span style="font-size: 1rem; font-weight: 400;">Mbps</span>
                </div>
                <div style="font-size: 0.9rem; opacity: 0.85; margin-top: 4px;">
                    Ping: {ping_avg_str} ms &bull; Gateway: 192.168.51.1
                </div>
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------------
# 3. KPI Metrics Row
# ----------------------------------------------------------------------------
total_checks = len(df)
ideal_checks = (df["network_status"] == "Ideal").sum()
uptime_pct = (ideal_checks / total_checks * 100) if total_checks > 0 else 0.0

raw_avg_dl = df["download_mbps"].dropna().mean() if "download_mbps" in df.columns else 0.0
raw_avg_ul = df["upload_mbps"].dropna().mean() if "upload_mbps" in df.columns else 0.0
avg_dl = 0.0 if (pd.isna(raw_avg_dl) or math.isnan(raw_avg_dl)) else float(raw_avg_dl)
avg_ul = 0.0 if (pd.isna(raw_avg_ul) or math.isnan(raw_avg_ul)) else float(raw_avg_ul)

episodes = compute_outage_episodes(df)
num_outages = len(episodes)
longest_outage_s = max([ep["duration_s"] for ep in episodes], default=0)
longest_outage_str = format_seconds_to_str(longest_outage_s)

# Total time OFF calculation from OFF rows
off_rows = df[df["computer_status"] == "OFF"]
total_off_s = 0
for _, r in off_rows.iterrows():
    note = str(r.get("note", ""))
    if "duration " in note:
        dur_part = note.split("duration ")[-1].strip()
        h, m, s = 0, 0, 0
        if "h" in dur_part:
            try:
                h = int(dur_part.split("h")[0].strip().split()[-1])
            except Exception:
                pass
        if "m" in dur_part:
            try:
                m = int(dur_part.split("m")[0].strip().split()[-1])
            except Exception:
                pass
        if "s" in dur_part:
            try:
                s = int(dur_part.split("s")[0].strip().split()[-1])
            except Exception:
                pass
        total_off_s += (h * 3600) + (m * 60) + s
    else:
        total_off_s += 300

total_off_str = format_seconds_to_str(total_off_s)

k1, k2, k3, k4, k5, k6 = st.columns(6)
with k1:
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-title">Uptime % (Ideal)</div>
            <div class="metric-value">{uptime_pct:.1f}%</div>
            <div class="metric-sub">{ideal_checks} / {total_checks} checks</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
with k2:
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-title">Avg Download</div>
            <div class="metric-value">{avg_dl:.1f} <span style="font-size: 1rem;">Mbps</span></div>
            <div class="metric-sub">Target: 100 Mbps</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
with k3:
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-title">Avg Upload</div>
            <div class="metric-value">{avg_ul:.1f} <span style="font-size: 1rem;">Mbps</span></div>
            <div class="metric-sub">Target: 100 Mbps</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
with k4:
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-title">Outages</div>
            <div class="metric-value">{num_outages}</div>
            <div class="metric-sub">Disruption episodes</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
with k5:
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-title">Longest Outage</div>
            <div class="metric-value">{longest_outage_str}</div>
            <div class="metric-sub">Max consecutive down</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
with k6:
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-title">Total Time OFF</div>
            <div class="metric-value">{total_off_str}</div>
            <div class="metric-sub">{len(off_rows)} power cuts</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

st.write("")

# ----------------------------------------------------------------------------
# 4. Download and Upload Line Chart (Plotly)
# ----------------------------------------------------------------------------
st.subheader("📈 Bandwidth History (Download & Upload)")

fig_speed = go.Figure()

fig_speed.add_trace(
    go.Scatter(
        x=df["datetime"],
        y=df["download_mbps"],
        mode="lines",
        name="Download (Mbps)",
        line=dict(color="#38bdf8", width=2.2),
        hovertemplate="<b>Download</b>: %{y:.2f} Mbps<br><b>Time</b>: %{x|%Y-%m-%d %H:%M}<extra></extra>",
    )
)

fig_speed.add_trace(
    go.Scatter(
        x=df["datetime"],
        y=df["upload_mbps"],
        mode="lines",
        name="Upload (Mbps)",
        line=dict(color="#c084fc", width=2.0),
        hovertemplate="<b>Upload</b>: %{y:.2f} Mbps<br><b>Time</b>: %{x|%Y-%m-%d %H:%M}<extra></extra>",
    )
)

# Reference Lines: Alert Threshold (85 Mbps) and Expected Speed (100 Mbps)
fig_speed.add_hline(
    y=100.0,
    line_dash="dot",
    line_color="#10b981",
    line_width=1.5,
    annotation_text="Expected Speed (100 Mbps)",
    annotation_position="top left",
)

fig_speed.add_hline(
    y=85.0,
    line_dash="dash",
    line_color="#ef4444",
    line_width=1.5,
    annotation_text="Alert Threshold (85 Mbps)",
    annotation_position="bottom left",
)

fig_speed.update_layout(
    margin=dict(l=20, r=20, t=30, b=20),
    height=380,
    hovermode="x unified",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    xaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.08)"),
    yaxis=dict(title="Speed (Mbps)", showgrid=True, gridcolor="rgba(255,255,255,0.08)", rangemode="tozero"),
)
st.plotly_chart(fig_speed, use_container_width=True)

# ----------------------------------------------------------------------------
# 5. Status Timeline with Blackout Gap Detection (> 7m)
# ----------------------------------------------------------------------------
st.subheader("⏱️ Status Timeline & Blackout Gaps")

timeline_items = []
prev_dt = None

for _, row in df.iterrows():
    curr_dt = row["datetime"]
    if prev_dt is not None:
        delta_s = (curr_dt - prev_dt).total_seconds()
        if delta_s > 420:  # Gap of more than 7 minutes
            gap_center = prev_dt + timedelta(seconds=delta_s / 2)
            timeline_items.append({
                "datetime": gap_center,
                "status_label": "No Data / Blackout",
                "color": "#64748b",
                "download_mbps": 0.0,
                "note": f"Data gap of {format_seconds_to_str(delta_s)}",
            })

    if row.get("computer_status") == "OFF":
        label = "Power Cut (OFF)"
        color = "#334155"
    else:
        st_val = row.get("network_status", "Ideal")
        label = st_val
        if st_val == "Ideal":
            color = "#10b981"
        elif st_val == "Low":
            color = "#f59e0b"
        else:
            color = "#ef4444"

    timeline_items.append({
        "datetime": curr_dt,
        "status_label": label,
        "color": color,
        "download_mbps": row.get("download_mbps", 0.0),
        "note": row.get("note", ""),
    })
    prev_dt = curr_dt

tl_df = pd.DataFrame(timeline_items)
color_map = {
    "Ideal": "#10b981",
    "Low": "#f59e0b",
    "Totally Down": "#ef4444",
    "Power Cut (OFF)": "#475569",
    "No Data / Blackout": "#94a3b8",
}

fig_timeline = px.scatter(
    tl_df,
    x="datetime",
    y="status_label",
    color="status_label",
    color_discrete_map=color_map,
    category_orders={"status_label": ["Ideal", "Low", "Totally Down", "Power Cut (OFF)", "No Data / Blackout"]},
    hover_data={"datetime": "|%Y-%m-%d %H:%M", "download_mbps": ":.2f", "note": True},
    height=250,
)
fig_timeline.update_traces(marker=dict(size=8, symbol="square"))
fig_timeline.update_layout(
    margin=dict(l=20, r=20, t=10, b=20),
    xaxis=dict(title="", showgrid=True, gridcolor="rgba(255,255,255,0.08)"),
    yaxis=dict(title="", categoryorder="array", categoryarray=["No Data / Blackout", "Power Cut (OFF)", "Totally Down", "Low", "Ideal"]),
    showlegend=True,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
)
st.plotly_chart(fig_timeline, use_container_width=True)

# ----------------------------------------------------------------------------
# 6. Ping Latency & Packet Loss
# ----------------------------------------------------------------------------
st.subheader("📶 Ping Latency & Packet Loss")

p_c1, p_c2 = st.columns(2)

with p_c1:
    fig_ping = go.Figure()
    fig_ping.add_trace(
        go.Scatter(
            x=df["datetime"],
            y=df["ping_avg_ms"],
            mode="lines",
            name="Ping Latency (ms)",
            line=dict(color="#f97316", width=2),
            hovertemplate="<b>Ping Latency</b>: %{y} ms<br><b>Time</b>: %{x|%Y-%m-%d %H:%M}<extra></extra>",
        )
    )
    fig_ping.update_layout(
        title="Ping Latency to Gateway (192.168.51.1)",
        margin=dict(l=20, r=20, t=40, b=20),
        height=300,
        xaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.08)"),
        yaxis=dict(title="Latency (ms)", rangemode="tozero", showgrid=True, gridcolor="rgba(255,255,255,0.08)"),
    )
    st.plotly_chart(fig_ping, use_container_width=True)

with p_c2:
    fig_loss = go.Figure()
    fig_loss.add_trace(
        go.Bar(
            x=df["datetime"],
            y=df["ping_loss_percent"],
            name="Ping Loss %",
            marker_color="#ef4444",
            hovertemplate="<b>Packet Loss</b>: %{y:.0f}%<br><b>Time</b>: %{x|%Y-%m-%d %H:%M}<extra></extra>",
        )
    )
    fig_loss.update_layout(
        title="Ping Packet Loss (%)",
        margin=dict(l=20, r=20, t=40, b=20),
        height=300,
        xaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.08)"),
        yaxis=dict(title="Loss %", range=[0, 105], showgrid=True, gridcolor="rgba(255,255,255,0.08)"),
    )
    st.plotly_chart(fig_loss, use_container_width=True)

# ----------------------------------------------------------------------------
# 7. Outage Episodes & Power Cut Table
# ----------------------------------------------------------------------------
st.subheader("⚠️ Outage Episodes & Disruption Log")

if episodes:
    ep_rows = []
    for ep in episodes:
        ep_rows.append({
            "Disruption Type": ep["type"],
            "Start Time": ep["start"].strftime(TS_FMT),
            "End Time": ep["end"].strftime(TS_FMT),
            "Duration": ep["duration_str"],
            "Lowest Download": f"{ep['min_dl']:.2f} Mbps" if ep["type"] != "Power Cut (PC OFF)" else "0.00 Mbps",
            "Notes / Context": " | ".join(ep["notes"]) if ep["notes"] else "None",
        })
    ep_df = pd.DataFrame(ep_rows)
    st.dataframe(ep_df, use_container_width=True, hide_index=True)
else:
    st.success("No network outages or power cuts detected in the selected time range! 🎉")

# Detailed list for Power Cut (OFF) events
if not off_rows.empty:
    with st.expander(f"⚡ Detailed Power Cut Episodes ({len(off_rows)} recorded)"):
        off_display = off_rows[["local_time", "note"]].copy()
        off_display.columns = ["Timestamp (Last Seen ON)", "Event Note"]
        st.dataframe(off_display, use_container_width=True, hide_index=True)

# ----------------------------------------------------------------------------
# 8. Raw Data Table with CSV Export
# ----------------------------------------------------------------------------
st.subheader("📋 Raw Data Records")

display_cols = [
    "local_time",
    "computer_status",
    "network_status",
    "download_mbps",
    "upload_mbps",
    "ping_status",
    "ping_avg_ms",
    "ping_loss_percent",
    "note",
]
available_cols = [c for c in display_cols if c in df.columns]
raw_display_df = df[available_cols].rename(columns={"local_time": "timestamp"}).sort_values("timestamp", ascending=False)

col_d1, col_d2 = st.columns([4, 1])
with col_d2:
    csv_bytes = raw_display_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="📥 Download CSV",
        data=csv_bytes,
        file_name=f"network_monitor_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        use_container_width=True,
    )

st.dataframe(raw_display_df, use_container_width=True, hide_index=True)
