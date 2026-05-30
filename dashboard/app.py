"""
Streamlit dashboard. Connects to the API and refreshes every 5 seconds.

Why Streamlit and not a SPA?
  • One file, one process, no build step.
  • The dashboard is a debugging surface, not a customer-facing UI.
  • Re-rendering the whole page every 5s is fine for a handful of
    metrics; we don't need WebSocket plumbing for this scope.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd
import requests
import streamlit as st

API = os.environ.get("STORE_INTEL_API", "http://api:8000")
DEFAULT_STORE = os.environ.get("STORE_INTEL_DEFAULT_STORE", "STORE_BLR_002")
REFRESH_SECONDS = int(os.environ.get("STORE_INTEL_REFRESH", "5"))

st.set_page_config(page_title="Store Intelligence", layout="wide", page_icon="🛒")


# ---------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------
def _get(path: str) -> Dict[str, Any]:
    try:
        r = requests.get(f"{API}{path}", timeout=5)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        return {"_error": str(e)}


# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------
def _render_health(health: Dict[str, Any]) -> None:
    status = health.get("status", "unknown")
    colour = {"ok": "🟢", "warning": "🟡", "degraded": "🔴"}.get(status, "⚪")
    st.write(
        f"{colour} **API status:** {status}  •  "
        f"uptime: {health.get('uptime_seconds', 0)}s"
    )
    stale = health.get("stale_feeds") or []
    if stale:
        st.error(
            f"STALE_FEED: {', '.join(stale)} — no events in the last 10 minutes."
        )


# ---------------------------------------------------------------------
# Top KPIs
# ---------------------------------------------------------------------
def _render_metrics(m: Dict[str, Any]) -> None:
    cols = st.columns(4)
    cols[0].metric("Unique visitors today", m.get("unique_visitors", 0))
    conv = m.get("conversion_rate", 0.0)
    cols[1].metric("Conversion rate", f"{conv * 100:.1f}%")
    cols[2].metric("Queue depth (now)", m.get("current_queue_depth", 0))
    cols[3].metric("Abandonment", f"{m.get('abandonment_rate', 0) * 100:.1f}%")


# ---------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------
def _render_anomalies(items: List[Dict[str, Any]]) -> None:
    if not items:
        st.success("No active anomalies.")
        return
    for a in items:
        sev = a["severity"]
        text = (
            f"**{a['type']}** — {a['description']}\n\n"
            f"_Suggested:_ {a['suggested_action']}"
        )
        if sev == "CRITICAL":
            st.error(text)
        elif sev == "WARN":
            st.warning(text)
        else:
            st.info(text)


# ---------------------------------------------------------------------
# Funnel — bar chart of the 4 stages with drop-off labels
# ---------------------------------------------------------------------
def _render_funnel(funnel: Dict[str, Any]) -> None:
    """
    Render the funnel as a horizontal bar chart with 4 stages and the
    drop-off % between each adjacent pair.

    We use Streamlit's built-in `st.bar_chart` (Vega-Lite under the
    hood) so we do not pull in a heavier plotting library.
    """
    stages = [
        ("1. Entry",         int(funnel.get("entry_count", 0)),
                              None),
        ("2. Zone Visit",    int(funnel.get("zone_visit_count", 0)),
                              float(funnel.get("entry_to_zone_dropoff_pct", 0.0))),
        ("3. Billing Queue", int(funnel.get("billing_queue_count", 0)),
                              float(funnel.get("zone_to_billing_dropoff_pct", 0.0))),
        ("4. Purchase",      int(funnel.get("purchase_count", 0)),
                              float(funnel.get("billing_to_purchase_dropoff_pct", 0.0))),
    ]

    df = pd.DataFrame({
        "Stage": [s[0] for s in stages],
        "Visitors": [s[1] for s in stages],
    })

    # Bar chart — horizontal so stage labels are readable.
    st.bar_chart(df, x="Stage", y="Visitors", horizontal=True,
                 use_container_width=True)

    # Stage table with drop-off % between stages.
    rows = []
    for stage, count, dropoff in stages:
        rows.append({
            "Stage": stage,
            "Visitors": count,
            "Drop-off from previous": f"{dropoff:.1f}%" if dropoff is not None else "—",
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


# ---------------------------------------------------------------------
# Heatmap
# ---------------------------------------------------------------------
def _render_heatmap(zones: List[Dict[str, Any]]) -> None:
    if not zones:
        st.write("_No zone data yet._")
        return
    df = pd.DataFrame(zones)
    # Round the dwell column for readability.
    if "avg_dwell_ms" in df.columns:
        df["avg_dwell_s"] = (df["avg_dwell_ms"] / 1000.0).round(1)
    st.dataframe(df, hide_index=True, use_container_width=True)
    if "normalized_score" in df.columns and "zone_id" in df.columns:
        st.bar_chart(df, x="zone_id", y="normalized_score",
                     use_container_width=True)


# ---------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------
st.title("🛒 Store Intelligence — live")
store_id = st.sidebar.text_input("Store ID", value=DEFAULT_STORE)
st.sidebar.caption(f"API: `{API}`  •  refresh every {REFRESH_SECONDS}s")

placeholder = st.empty()

# Single render pass; st.rerun at the bottom forces the loop.
with placeholder.container():
    health = _get("/health")
    if "_error" in health:
        st.error(f"Cannot reach API at {API}: {health['_error']}")
    else:
        _render_health(health)

    st.subheader("Live metrics")
    metrics = _get(f"/stores/{store_id}/metrics")
    if "_error" in metrics:
        st.error(metrics["_error"])
    else:
        _render_metrics(metrics)

    st.subheader("Conversion funnel")
    funnel = _get(f"/stores/{store_id}/funnel")
    if "_error" in funnel:
        st.error(funnel["_error"])
    else:
        _render_funnel(funnel)

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("Anomalies")
        anom = _get(f"/stores/{store_id}/anomalies")
        if "_error" in anom:
            st.error(anom["_error"])
        else:
            _render_anomalies(anom.get("anomalies", []))

    with col2:
        st.subheader("Zone heatmap")
        heatmap = _get(f"/stores/{store_id}/heatmap")
        if "_error" not in heatmap:
            _render_heatmap(heatmap.get("zones", []))

    st.caption(f"Last refresh: {datetime.utcnow().isoformat()}Z")

time.sleep(REFRESH_SECONDS)
st.rerun()
