"""
app.py - Streamlit Monitoring Dashboard for Network & Power Monitor
Clean, communication-focused monitoring dashboard optimized for mobile & desktop,
defaulting to Today's view, with native Light/Dark theme support and zero sidebar clutter.
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
TS_FMT = "%Y-%m-%d %I:%M:%S %p"

st.set_page_config(
    page_title="Network & Power Monitor",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ----------------------------------------------------------------------------
# Minimal Adaptive CSS (High-contrast status banner & clean typography)
# ----------------------------------------------------------------------------
st.markdown(
    """
    <style>
    /* Clean metric value typography */
    [data-testid="stMetricValue"] {
        font-weight: 700 !important;
        font-size: 1.85rem !important;
    }
    [data-testid="stMetricLabel"] {
        font-weight: 600 !important;
        font-size: 0.85rem !important;
        text-transform: uppercase !important;
        letter-spacing: 0.04em !important;
    }

    /* Live Status Banner - High Contrast in both Dark and Light Themes */
    .status-banner {
        border-radius: 12px;
        padding: 18px 24px;
        margin-bottom: 14px;
        color: #ffffff !important;
        box-shadow: 0 4px 14px rgba(0, 0, 0, 0.08);
    }
    .status-banner h2, .status-banner div, .status-banner span, .status-banner strong {
        color: #ffffff !important;
    }
    .banner-ideal {
        background: linear-gradient(135deg, #065f46 0%, #047857 60%, #059669 100%);
        border: 1px solid #10b981;
    }
    .banner-low {
        background: linear-gradient(135deg, #92400e 0%, #b45309 60%, #d97706 100%);
        border: 1px solid #f59e0b;
    }
    .banner-down {
        background: linear-gradient(135deg, #991b1b 0%, #b91c1c 60%, #dc2626 100%);
        border: 1px solid #ef4444;
    }
    .banner-off {
        background: linear-gradient(135deg, #1e293b 0%, #334155 60%, #475569 100%);
        border: 1px solid #64748b;
    }
    .status-tag {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 9999px;
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.05em;
        text-transform: uppercase;
        background: rgba(255, 255, 255, 0.22);
    }

    /* Stale Offline Notice */
    .stale-box {
        background: linear-gradient(135deg, #7f1d1d 0%, #991b1b 100%);
        border: 1px solid #f87171;
        border-radius: 10px;
        padding: 14px 18px;
        margin-bottom: 14px;
        color: #ffffff !important;
    }
    .stale-box h4, .stale-box div, .stale-box strong {
        color: #ffffff !important;
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

    if not app_pwd:
        return True

    if st.session_state.get("authenticated", False):
        return True

    st.markdown("### 🔒 Network Monitor - Authentication")
    st.write("This dashboard is password-protected. Please enter your access password.")
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
# Cached Data Loader (5m TTL per table and range)
# ----------------------------------------------------------------------------
@st.cache_data(ttl=300)
def cached_load_data(supabase_url, anon_key, table_name, start_iso, end_iso):
    if not supabase_url or not anon_key:
        return None, pd.DataFrame(), "None", (
            "SUPABASE_URL and SUPABASE_ANON_KEY are not configured. "
            "Please configure them in Streamlit Cloud Secrets."
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
# Header, Office Switcher & Filter Controls
# ----------------------------------------------------------------------------
col_title, col_ref = st.columns([5, 1])
with col_title:
    st.title("⚡ Network & Power Monitor")
with col_ref:
    st.write("")
    if st.button("🔄 Refresh", use_container_width=True, help="Fetch latest readings"):
        st.cache_data.clear()
        st.rerun()

ctrl_left, ctrl_right = st.columns([1, 1])

with ctrl_left:
    st.caption("SELECT OFFICE")
    if hasattr(st, "segmented_control"):
        office_choice = st.segmented_control(
            "Office Location",
            options=["🏢 New Office", "🏚️ Old Office"],
            default="🏢 New Office",
            label_visibility="collapsed",
        )
    else:
        office_choice = st.radio(
            "Office Location",
            options=["🏢 New Office", "🏚️ Old Office"],
            index=0,
            horizontal=True,
            label_visibility="collapsed",
        )
    if not office_choice:
        office_choice = "🏢 New Office"

with ctrl_right:
    st.caption("SELECT TIME RANGE")
    if hasattr(st, "segmented_control"):
        range_option = st.segmented_control(
            "Time Range",
            options=["Today", "7 Days", "30 Days", "Custom"],
            default="Today",  # Always opens in Today's view by default
            label_visibility="collapsed",
        )
    else:
        range_option = st.radio(
            "Time Range",
            options=["Today", "7 Days", "30 Days", "Custom"],
            index=0,  # "Today" is default
            horizontal=True,
            label_visibility="collapsed",
        )
    if not range_option:
        range_option = "Today"

office_table_map = {
    "🏢 New Office": "network_log",
    "🏚️ Old Office": "network_log_old_office",
}
current_table = office_table_map.get(office_choice, "network_log")

now_kolkata = datetime.now(KOLKATA_TZ)
max_date = now_kolkata.date()

selected_start_dt = None
selected_end_dt = None

if range_option == "Today":
    selected_start_dt = datetime.combine(max_date, datetime.min.time(), tzinfo=KOLKATA_TZ)
    selected_end_dt = datetime.combine(max_date, datetime.max.time(), tzinfo=KOLKATA_TZ)
elif range_option == "7 Days":
    selected_start_dt = datetime.combine(max_date - timedelta(days=7), datetime.min.time(), tzinfo=KOLKATA_TZ)
    selected_end_dt = datetime.combine(max_date, datetime.max.time(), tzinfo=KOLKATA_TZ)
elif range_option == "30 Days":
    selected_start_dt = datetime.combine(max_date - timedelta(days=30), datetime.min.time(), tzinfo=KOLKATA_TZ)
    selected_end_dt = datetime.combine(max_date, datetime.max.time(), tzinfo=KOLKATA_TZ)
elif range_option == "Custom":
    c_d1, c_d2 = st.columns(2)
    with c_d1:
        s_date = st.date_input("From Date", value=max_date - timedelta(days=7), max_value=max_date)
    with c_d2:
        e_date = st.date_input("To Date", value=max_date, max_value=max_date)
    if s_date > e_date:
        st.error("Start Date cannot be after End Date.")
        st.stop()
    selected_start_dt = datetime.combine(s_date, datetime.min.time(), tzinfo=KOLKATA_TZ)
    selected_end_dt = datetime.combine(e_date, datetime.max.time(), tzinfo=KOLKATA_TZ)

# Resolve credentials
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

start_iso = selected_start_dt.isoformat() if selected_start_dt else None
end_iso = selected_end_dt.isoformat() if selected_end_dt else None

latest_row, df, source_label, error_msg = cached_load_data(
    supabase_url=supabase_url,
    anon_key=anon_key,
    table_name=current_table,
    start_iso=start_iso,
    end_iso=end_iso,
)

if error_msg and (df is None or df.empty):
    if "PGRST205" in error_msg or "Could not find the table" in error_msg:
        st.warning(
            f"⚠️ Supabase table **`{current_table}`** for **{office_choice}** has not been created yet!\n\n"
            f"Please run the SQL schema in `supabase_setup.sql` in your Supabase SQL editor."
        )
    else:
        st.error(error_msg)
    st.stop()

if df is None or df.empty:
    st.info(
        f"No records found in table **`{current_table}`** for **{office_choice}** in this time range. "
        f"Ensure `network_monitor.py` on that office PC is running with `SUPABASE_TABLE = \"{current_table}\"`."
    )
    st.stop()

# ----------------------------------------------------------------------------
# 2. Live Status Banner & Stale Outage Warning
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
    last_updated_str = newest_dt.strftime("%b %d, %I:%M:%S %p")
    # If now - newest ts > 12 minutes (720 seconds), display prominent offline alert
    if elapsed_seconds > 720:
        is_stale = True

if is_stale:
    time_ago_str = format_seconds_to_str(elapsed_seconds)
    st.markdown(
        f"""
        <div class="stale-box">
            <h4 style="margin:0 0 4px 0; font-size:1.15rem;">⚠️ Outage Warning: PC or Network Offline</h4>
            <div style="font-size:0.95rem;">
                No data received since <strong>{last_updated_str}</strong> ({time_ago_str} ago). 
                The office PC, power supply, or internet connection appears to be down.
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

if computer_status == "OFF":
    banner_class = "banner-off"
    status_icon = "⚫"
    status_label = "PC OFF (POWER CUT)"
elif current_net_status == "Low":
    banner_class = "banner-low"
    status_icon = "🟡"
    status_label = "LOW SPEED ALERT"
elif current_net_status == "Totally Down":
    banner_class = "banner-down"
    status_icon = "🔴"
    status_label = "TOTALLY DOWN"
else:
    banner_class = "banner-ideal"
    status_icon = "🟢"
    status_label = "NORMAL (IDEAL)"

st.markdown(
    f"""
    <div class="status-banner {banner_class}">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
            <div>
                <span class="status-tag">{office_choice} &bull; {computer_status} &bull; {ping_status}</span>
                <h2 style="margin: 6px 0 2px 0; font-size: 1.85rem; font-weight: 800;">
                    {status_icon} {status_label}
                </h2>
                <div style="font-size: 0.88rem; opacity: 0.9;">
                    Last Reading: <strong>{last_updated_str}</strong> ({format_seconds_to_str(elapsed_seconds)} ago)
                </div>
            </div>
            <div style="text-align: right; min-width: 200px;">
                <div style="font-size: 1.6rem; font-weight: 700;">
                    ↓ {dl_speed:.1f} <span style="font-size: 0.95rem; font-weight: 400;">Mbps</span>
                    &nbsp;&nbsp;
                    ↑ {ul_speed:.1f} <span style="font-size: 0.95rem; font-weight: 400;">Mbps</span>
                </div>
                <div style="font-size: 0.88rem; opacity: 0.9; margin-top: 2px;">
                    Ping Latency: <strong>{ping_avg_str} ms</strong>
                </div>
            </div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------------------
# 3. KPI Metrics (Using Default Streamlit Elements for Native Theme & Mobile)
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

# Total PC OFF duration
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

m1, m2, m3, m4 = st.columns(4)

with m1:
    with st.container(border=True):
        st.metric(
            label="⚡ Network Uptime",
            value=f"{uptime_pct:.1f}%",
            delta=f"{ideal_checks}/{total_checks} normal checks",
            help="Percentage of check cycles operating at normal speed (>= 85 Mbps)",
        )

with m2:
    with st.container(border=True):
        dl_diff = avg_dl - 85.0
        st.metric(
            label="📥 Avg Download",
            value=f"{avg_dl:.1f} Mbps",
            delta=f"{dl_diff:+.1f} Mbps vs 85 Alert",
            help="Average download speed across selected period. Plan speed: 100 Mbps",
        )

with m3:
    with st.container(border=True):
        st.metric(
            label="📤 Avg Upload",
            value=f"{avg_ul:.1f} Mbps",
            delta="Plan: 100 Mbps",
            delta_color="off",
            help="Average upload speed across selected period. Plan speed: 100 Mbps",
        )

with m4:
    with st.container(border=True):
        disrupt_sub = f"{longest_outage_str} max outage" if num_outages > 0 else "All Normal"
        st.metric(
            label="⚠️ Disruptions",
            value=f"{num_outages}",
            delta=disrupt_sub,
            delta_color="inverse" if num_outages > 0 else "normal",
            help="Total number of network outages, severe speed drops, or power cuts",
        )

st.write("")

# ----------------------------------------------------------------------------
# 4. Communicative Visualizations & Diagnostic Analytics
# ----------------------------------------------------------------------------
tab_speed, tab_outage, tab_data = st.tabs([
    "📈 Bandwidth & Quality",
    "⚠️ Outages & Power Cuts",
    "📋 Raw Log & Export",
])

# Plotly Common Style for Native Dark & Light Themes
PLOT_CONFIG = {"responsive": True, "displayModeBar": False}
LAYOUT_COMMON = dict(
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    font=dict(family="sans serif"),
    margin=dict(l=10, r=10, t=30, b=10),
    xaxis=dict(
        showgrid=True,
        gridcolor="rgba(128, 128, 128, 0.16)",
        tickformat="%b %d\n%I:%M %p",
    ),
)


with tab_speed:
    # 1. Main Bandwidth Over Time Chart
    st.markdown("#### 🚀 Speed Delivery vs 100 Mbps Contract Plan")
    st.caption("Continuous 5-minute speed tests. Speed below the dashed red line triggers email alerts.")

    fig_speed = go.Figure()

    # Red shaded zone for SLA violation (< 85 Mbps)
    fig_speed.add_hrect(
        y0=0,
        y1=85.0,
        fillcolor="rgba(239, 68, 68, 0.07)",
        layer="below",
        line_width=0,
    )

    # Download Trace
    fig_speed.add_trace(
        go.Scatter(
            x=df["datetime"],
            y=df["download_mbps"],
            mode="lines+markers",
            name="Download",
            marker=dict(size=4),
            line=dict(color="#0284c7", width=2.5),
            hovertemplate="<b>Download</b>: %{y:.2f} Mbps<br><b>Time</b>: %{x|%b %d, %I:%M %p}<extra></extra>",
        )
    )

    # Upload Trace
    fig_speed.add_trace(
        go.Scatter(
            x=df["datetime"],
            y=df["upload_mbps"],
            mode="lines",
            name="Upload",
            line=dict(color="#8b5cf6", width=2.0),
            hovertemplate="<b>Upload</b>: %{y:.2f} Mbps<br><b>Time</b>: %{x|%b %d, %I:%M %p}<extra></extra>",
        )
    )

    # Target Plan Line (100 Mbps)
    fig_speed.add_hline(
        y=100.0,
        line_dash="dot",
        line_color="#10b981",
        line_width=1.5,
        annotation_text="Target Plan (100 Mbps)",
        annotation_position="top left",
    )

    # Alert Threshold Line (85 Mbps)
    fig_speed.add_hline(
        y=85.0,
        line_dash="dash",
        line_color="#ef4444",
        line_width=1.5,
        annotation_text="Alert Level (< 85 Mbps)",
        annotation_position="bottom left",
    )

    fig_speed.update_layout(
        **LAYOUT_COMMON,
        height=340,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        yaxis=dict(
            title="Speed (Mbps)",
            rangemode="tozero",
            showgrid=True,
            gridcolor="rgba(128, 128, 128, 0.16)",
        ),
    )
    st.plotly_chart(fig_speed, use_container_width=True, config=PLOT_CONFIG)

    st.write("")

    # 2. Additional Communicative Visualizations: Hourly Performance & SLA Quality Donut
    c_vis1, c_vis2 = st.columns(2)

    with c_vis1:
        st.markdown("#### ⏰ Hourly Speed Performance")
        st.caption("Average download speed by hour of the day. Highlights peak-hour office slowdowns.")

        # Compute hourly averages
        df_hourly = df.copy()
        df_hourly["hour_int"] = df_hourly["datetime"].dt.hour
        df_hourly["hour_label"] = df_hourly["datetime"].dt.strftime("%I %p").str.lstrip("0")
        hourly_grp = (
            df_hourly.groupby(["hour_int", "hour_label"], as_index=False)
            .agg({"download_mbps": "mean", "upload_mbps": "mean"})
            .sort_values("hour_int")
        )

        if not hourly_grp.empty:
            # Color bars according to SLA thresholds
            bar_colors = [
                "#10b981" if v >= 85 else ("#f59e0b" if v >= 50 else "#ef4444")
                for v in hourly_grp["download_mbps"]
            ]

            fig_hourly = go.Figure()
            fig_hourly.add_trace(
                go.Bar(
                    x=hourly_grp["hour_label"],
                    y=hourly_grp["download_mbps"],
                    marker=dict(color=bar_colors, opacity=0.9),
                    hovertemplate="<b>%{x}</b><br>Avg Download: %{y:.1f} Mbps<extra></extra>",
                    name="Download",
                )
            )
            fig_hourly.add_hline(
                y=85.0,
                line_dash="dash",
                line_color="#ef4444",
                line_width=1.2,
                annotation_text="85 Mbps Alert",
                annotation_position="bottom right",
            )
            fig_hourly.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="sans serif"),
                margin=dict(l=10, r=10, t=20, b=10),
                height=260,
                xaxis=dict(showgrid=False, title=""),
                yaxis=dict(
                    title="Avg Mbps",
                    rangemode="tozero",
                    showgrid=True,
                    gridcolor="rgba(128, 128, 128, 0.16)",
                ),
            )
            st.plotly_chart(fig_hourly, use_container_width=True, config=PLOT_CONFIG)
        else:
            st.info("Insufficient hourly data for this selection.")

    with c_vis2:
        st.markdown("#### 🎯 SLA Speed Compliance")
        st.caption("Percentage of check cycles meeting contract plan vs degraded periods.")

        def categorize_sla(val):
            if pd.isna(val) or val <= 0:
                return "Critical (< 50 Mbps)"
            elif val < 50:
                return "Critical (< 50 Mbps)"
            elif val < 85:
                return "Degraded (50-85 Mbps)"
            elif val < 95:
                return "Good (85-95 Mbps)"
            else:
                return "Optimal (>= 95 Mbps)"

        sla_series = df["download_mbps"].apply(categorize_sla).value_counts()
        sla_color_map = {
            "Optimal (>= 95 Mbps)": "#10b981",
            "Good (85-95 Mbps)": "#0284c7",
            "Degraded (50-85 Mbps)": "#f59e0b",
            "Critical (< 50 Mbps)": "#ef4444",
        }

        fig_donut = go.Figure(
            data=[
                go.Pie(
                    labels=sla_series.index.tolist(),
                    values=sla_series.values.tolist(),
                    hole=0.55,
                    marker=dict(colors=[sla_color_map.get(k, "#64748b") for k in sla_series.index]),
                    textinfo="percent+label",
                    textposition="inside",
                    insidetextorientation="horizontal",
                    hovertemplate="<b>%{label}</b>: %{value} checks (%{percent})<extra></extra>",
                )
            ]
        )
        fig_donut.update_layout(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(family="sans serif"),
            margin=dict(l=10, r=10, t=20, b=10),
            height=260,
            showlegend=False,
        )
        st.plotly_chart(fig_donut, use_container_width=True, config=PLOT_CONFIG)

    st.write("")

    # 3. Latency & Packet Loss
    st.markdown("#### 📶 Latency & Packet Stability")
    st.caption("Router gateway ping latency in milliseconds, flagging any packet drop events.")

    fig_latency = go.Figure()

    fig_latency.add_trace(
        go.Scatter(
            x=df["datetime"],
            y=df["ping_avg_ms"],
            mode="lines",
            name="Ping Latency (ms)",
            line=dict(color="#f59e0b", width=2.0),
            hovertemplate="<b>Ping</b>: %{y:.0f} ms<br><b>Time</b>: %{x|%b %d, %I:%M %p}<extra></extra>",
        )
    )

    loss_events = df[df["ping_loss_percent"] > 0]
    if not loss_events.empty:
        fig_latency.add_trace(
            go.Scatter(
                x=loss_events["datetime"],
                y=loss_events["ping_avg_ms"].fillna(0),
                mode="markers",
                name="Packet Loss Event",
                marker=dict(color="#ef4444", size=9, symbol="x"),
                hovertemplate="<b>Loss</b>: %{customdata:.0f}%<br><b>Time</b>: %{x|%b %d, %I:%M %p}<extra></extra>",
                customdata=loss_events["ping_loss_percent"],
            )
        )

    fig_latency.update_layout(
        **LAYOUT_COMMON,
        height=240,
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        yaxis=dict(
            title="Ping Latency (ms)",
            rangemode="tozero",
            showgrid=True,
            gridcolor="rgba(128, 128, 128, 0.16)",
        ),
    )
    st.plotly_chart(fig_latency, use_container_width=True, config=PLOT_CONFIG)


with tab_outage:
    # 1. Outage Metrics & Cause Distribution
    st.markdown("#### ⚠️ Outages & Power Cut Diagnostics")
    st.caption("Distinguishes whether downtime was caused by power cuts (electricity) or ISP line disconnection.")

    col_o1, col_o2 = st.columns([1, 1])

    with col_o1:
        st.markdown("##### 🔍 Downtime Root Cause")
        # Categorize causes
        cause_counts = {}
        if not off_rows.empty:
            cause_counts["⚡ Power Cut (PC OFF)"] = len(off_rows)
        down_rows = df[df["network_status"] == "Totally Down"]
        if not down_rows.empty:
            cause_counts["🔴 ISP Line Cut (Down)"] = len(down_rows)
        low_rows = df[df["network_status"] == "Low"]
        if not low_rows.empty:
            cause_counts["🟡 Bandwidth Throttled (Low)"] = len(low_rows)

        if cause_counts:
            cause_colors = {
                "⚡ Power Cut (PC OFF)": "#475569",
                "🔴 ISP Line Cut (Down)": "#ef4444",
                "🟡 Bandwidth Throttled (Low)": "#f59e0b",
            }
            fig_cause = go.Figure(
                data=[
                    go.Pie(
                        labels=list(cause_counts.keys()),
                        values=list(cause_counts.values()),
                        hole=0.55,
                        marker=dict(colors=[cause_colors.get(k, "#64748b") for k in cause_counts.keys()]),
                        textinfo="percent+label",
                        hovertemplate="<b>%{label}</b>: %{value} events<extra></extra>",
                    )
                ]
            )
            fig_cause.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                font=dict(family="sans serif"),
                margin=dict(l=10, r=10, t=10, b=10),
                height=220,
                showlegend=False,
            )
            st.plotly_chart(fig_cause, use_container_width=True, config=PLOT_CONFIG)
        else:
            st.success("🎉 Zero disruptions or power cuts detected in this period!")

    with col_o2:
        st.markdown("##### ⏱️ Disruption Summary")
        with st.container(border=True):
            st.write(f"• **Total Recorded Disruptions:** {num_outages}")
            st.write(f"• **Longest Disruption:** {longest_outage_str}")
            st.write(f"• **Total Time PC was OFF (Power Cuts):** {total_off_str}")
            st.write(f"• **Uptime Percentage:** {uptime_pct:.1f}%")

    st.write("")

    # 2. Timeline Status Strip
    st.markdown("##### 📊 Status Strip Timeline")
    timeline_items = []
    prev_dt = None

    for _, row in df.iterrows():
        curr_dt = row["datetime"]
        if prev_dt is not None:
            delta_s = (curr_dt - prev_dt).total_seconds()
            if delta_s > 420:
                gap_center = prev_dt + timedelta(seconds=delta_s / 2)
                timeline_items.append({
                    "datetime": gap_center,
                    "status_label": "Blackout / Offline",
                    "color": "#64748b",
                    "download_mbps": 0.0,
                    "note": f"Offline gap of {format_seconds_to_str(delta_s)}",
                })

        if row.get("computer_status") == "OFF":
            label = "Power Cut (PC OFF)"
            color = "#334155"
        else:
            st_val = row.get("network_status", "Ideal")
            label = st_val
            if st_val == "Ideal":
                label = "Normal (Ideal)"
                color = "#10b981"
            elif st_val == "Low":
                label = "Low Speed"
                color = "#f59e0b"
            else:
                label = "Totally Down"
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
        "Normal (Ideal)": "#10b981",
        "Low Speed": "#f59e0b",
        "Totally Down": "#ef4444",
        "Power Cut (PC OFF)": "#475569",
        "Blackout / Offline": "#94a3b8",
    }

    fig_timeline = px.scatter(
        tl_df,
        x="datetime",
        y="status_label",
        color="status_label",
        color_discrete_map=color_map,
        category_orders={"status_label": ["Normal (Ideal)", "Low Speed", "Totally Down", "Power Cut (PC OFF)", "Blackout / Offline"]},
        hover_data={"datetime": "|%b %d, %I:%M %p", "download_mbps": ":.2f", "note": True},
        height=200,
    )
    fig_timeline.update_traces(marker=dict(size=8, symbol="square"))
    fig_timeline.update_layout(
        **LAYOUT_COMMON,
        height=200,
        yaxis=dict(
            title="",
            categoryorder="array",
            categoryarray=["Blackout / Offline", "Power Cut (PC OFF)", "Totally Down", "Low Speed", "Normal (Ideal)"],
            showgrid=True,
            gridcolor="rgba(128, 128, 128, 0.16)",
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig_timeline, use_container_width=True, config=PLOT_CONFIG)

    # 3. Incident Breakdown Table
    st.markdown("##### 📋 Recorded Incidents Detail")
    if episodes:
        ep_rows = []
        for ep in episodes:
            ep_rows.append({
                "Event Type": ep["type"],
                "Start Time (12h)": ep["start"].strftime("%b %d, %I:%M:%S %p"),
                "End Time (12h)": ep["end"].strftime("%b %d, %I:%M:%S %p"),
                "Duration": ep["duration_str"],
                "Lowest Speed": f"{ep['min_dl']:.1f} Mbps" if ep["type"] != "Power Cut (PC OFF)" else "0.0 Mbps",
                "Cause / Context": " | ".join(ep["notes"]) if ep["notes"] else "None",
            })
        ep_df = pd.DataFrame(ep_rows)
        st.dataframe(
            ep_df,
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.success("🎉 No outages recorded in this period.")

    if not off_rows.empty:
        with st.expander(f"⚡ Detailed Power Cut Log ({len(off_rows)} events)"):
            off_display = off_rows[["local_time", "note"]].copy()
            off_display.columns = ["Timestamp (12h)", "Event Details"]
            st.dataframe(off_display, use_container_width=True, hide_index=True)


with tab_data:
    st.markdown("#### 📋 Raw Monitoring Log")
    st.caption("Inspect and export granular 5-minute cycle readings.")

    col_filter, col_dl = st.columns([3, 1])

    with col_filter:
        status_filter = st.selectbox(
            "Filter Records by Status",
            options=["All Records", "Normal Only", "Disruptions Only (Low / Down)", "Power Cuts (OFF) Only"],
            index=0,
            label_visibility="collapsed",
        )

    filtered_df = df.copy()
    if status_filter == "Normal Only":
        filtered_df = filtered_df[filtered_df["network_status"] == "Ideal"]
    elif status_filter == "Disruptions Only (Low / Down)":
        filtered_df = filtered_df[filtered_df["network_status"].isin(["Low", "Totally Down"])]
    elif status_filter == "Power Cuts (OFF) Only":
        filtered_df = filtered_df[filtered_df["computer_status"] == "OFF"]

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
    available_cols = [c for c in display_cols if c in filtered_df.columns]
    raw_display_df = (
        filtered_df[available_cols]
        .rename(
            columns={
                "local_time": "Timestamp (12h)",
                "computer_status": "PC",
                "network_status": "Status",
                "download_mbps": "Down (Mbps)",
                "upload_mbps": "Up (Mbps)",
                "ping_status": "Ping",
                "ping_avg_ms": "Latency (ms)",
                "ping_loss_percent": "Loss %",
                "note": "Note",
            }
        )
        .sort_values("Timestamp (12h)", ascending=False)
    )

    with col_dl:
        csv_bytes = raw_display_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="📥 Export CSV",
            data=csv_bytes,
            file_name=f"network_monitor_{current_table}_{datetime.now().strftime('%Y%m%d_%I%M%p')}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    st.dataframe(
        raw_display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Down (Mbps)": st.column_config.NumberColumn("Down (Mbps)", format="%.2f"),
            "Up (Mbps)": st.column_config.NumberColumn("Up (Mbps)", format="%.2f"),
            "Latency (ms)": st.column_config.NumberColumn("Latency (ms)", format="%.0f"),
            "Loss %": st.column_config.NumberColumn("Loss %", format="%.0f%%"),
        },
    )
