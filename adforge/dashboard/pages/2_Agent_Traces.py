"""Agent Traces page — inspect agent runs + audit events."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from adforge.dashboard import api_client as api

st.title("Agent Traces")

try:
    campaigns = api.list_campaigns()
except Exception as e:
    st.error(f"Failed to load campaigns: {e}")
    campaigns = []

if not campaigns:
    st.info("No campaigns yet — create one on the Campaigns page.")
    st.stop()

campaign_id = st.selectbox(
    "Campaign",
    options=[c["id"] for c in campaigns],
    format_func=lambda cid: next(c["name"] for c in campaigns if c["id"] == cid),
)

agent_filter = st.selectbox(
    "Filter by agent",
    options=["(all)", "research", "creative_strategist", "media_planner",
             "compliance", "kpi_analytics", "roi_calculator", "optimization"],
)

try:
    runs = api.get_agent_runs(
        campaign_id,
        agent_name=None if agent_filter == "(all)" else agent_filter,
    )
except Exception as e:
    st.error(f"Failed to load runs: {e}")
    runs = {"runs": []}

# Summary metrics
col1, col2, col3, col4 = st.columns(4)
run_list = runs.get("runs", [])
with col1:
    st.metric("Total Runs", len(run_list))
with col2:
    succeeded = sum(1 for r in run_list if r["status"] == "succeeded")
    st.metric("Succeeded", succeeded)
with col3:
    failed = sum(1 for r in run_list if r["status"] == "failed")
    st.metric("Failed", failed)
with col4:
    total_tokens = sum(r.get("usage", {}).get("total_tokens", 0) for r in run_list)
    st.metric("Total Tokens", f"{total_tokens:,}")

st.divider()

# Runs table
if run_list:
    df = pd.DataFrame([
        {
            "Agent": r["agent_name"],
            "Status": r["status"],
            "Started": r["started_at"],
            "Duration (s)": r.get("duration_seconds"),
            "Tokens": r.get("usage", {}).get("total_tokens", 0),
            "Cost ($)": r.get("usage", {}).get("cost_usd", 0),
        }
        for r in run_list
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Drill-down into a single run
    st.divider()
    st.subheader("Inspect a single run")
    selected_idx = st.selectbox(
        "Select run",
        options=range(len(run_list)),
        format_func=lambda i: f"#{i} {run_list[i]['agent_name']} — {run_list[i]['status']}",
    )
    run = run_list[selected_idx]
    tab1, tab2, tab3 = st.tabs(["Input", "Output", "Usage"])
    with tab1:
        st.json(run["input_payload"])
    with tab2:
        st.json(run["output_payload"])
        if run.get("error"):
            st.error(run["error"])
    with tab3:
        st.json(run.get("usage", {}))

    # Audit events
    st.divider()
    st.subheader("Audit log (recent 100 events)")
    try:
        events = api.get_audit_events(campaign_id)
        ev_df = pd.DataFrame([
            {
                "Time": e["occurred_at"],
                "Type": e["event_type"],
                "Agent": e.get("agent_name") or "-",
                "Summary": _summarise(e["payload"]),
            }
            for e in events.get("events", [])
        ])
        st.dataframe(ev_df, use_container_width=True, hide_index=True)
    except Exception as e:
        st.warning(f"Could not load audit events: {e}")
else:
    st.info("No agent runs yet. Run a campaign from the Campaigns page.")


def _summarise(payload: dict) -> str:
    """Short summary of an audit event payload for the table."""
    if "error" in payload:
        return f"ERROR: {payload['error'][:80]}"
    if "duration_seconds" in payload:
        return f"duration={payload['duration_seconds']:.1f}s ok={payload.get('ok')}"
    if "model" in payload:
        return f"model={payload['model']} msgs={payload.get('message_count', 0)}"
    return json.dumps(payload)[:80] if payload else ""


import json as _json  # late import for the helper above
