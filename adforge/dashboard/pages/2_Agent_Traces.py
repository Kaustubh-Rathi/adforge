"""Agent Traces page — inspect agent runs + audit events."""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import Any

import streamlit as st

from adforge.dashboard import api_client as api
from adforge.dashboard.theme import (
    af_badge,
    af_caption,
    af_empty,
    af_metric,
    af_note,
    af_section,
    af_table,
    inject_css,
)

inject_css()
api.tenant_switcher()

#: Run statuses are not campaign statuses; map them onto the closest badge token
#: so a succeeded run reads green and a failed run reads red, with the raw
#: status kept as the pill label.
_RUN_BADGE_TOKENS: dict[str, str] = {
    "succeeded": "approved",
    "success": "approved",
    "running": "reviewing",
    "pending": "reviewing",
    "failed": "failed",
    "error": "failed",
    "timeout": "failed",
}


def _summarise(payload: dict[str, Any]) -> str:
    """Short summary of an audit event payload for the table."""
    if "error" in payload:
        return f"ERROR: {payload['error'][:80]}"
    if "duration_seconds" in payload:
        return f"duration={payload['duration_seconds']:.1f}s ok={payload.get('ok')}"
    if "model" in payload:
        return f"model={payload['model']} msgs={payload.get('message_count', 0)}"
    return json.dumps(payload)[:80] if payload else ""


def _fmt_ts(raw: Any) -> str:
    """Render an ISO timestamp as a compact, readable stamp."""
    text = str(raw or "")
    if not text:
        return "-"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M:%S")
    except ValueError:
        return text


def _fmt_duration(value: Any) -> str:
    """Sub-second runs read as ``<0.1``; a missing duration reads as an em dash."""
    if value is None:
        return "—"
    seconds = float(value)
    if seconds < 0.1:
        return "<0.1"
    return f"{seconds:.1f}"


def _run_badge(status: str) -> str:
    token = _RUN_BADGE_TOKENS.get(str(status or "").lower(), str(status or ""))
    label = str(status or "unknown").replace("_", " ").strip().title()
    return af_badge(token, label)


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


af_section("Agent traces", "Every agent run for a campaign — outcome, cost and the audit trail.")

try:
    campaigns = api.list_campaigns()
except Exception as e:
    af_note(f"<b>Failed to load campaigns.</b> {_esc(e)}", tone="bad")
    campaigns = []

if not campaigns:
    af_empty(
        "No campaigns in this tenant",
        "Create a brief on the Campaigns page, then run the agent graph.",
        hint="make seed",
    )
    st.stop()

campaign_id = st.selectbox(
    "Campaign",
    options=[c["id"] for c in campaigns],
    format_func=lambda cid: next(c["name"] for c in campaigns if c["id"] == cid),
)

filter_col, status_col = st.columns(2)
with filter_col:
    agent_filter = st.selectbox(
        "Filter by agent",
        options=[
            "(all)",
            "research",
            "creative_strategist",
            "media_planner",
            "compliance",
            "kpi_analytics",
            "roi_calculator",
            "optimization",
        ],
    )
with status_col:
    status_filter = st.selectbox("Filter by status", options=["(all)", "succeeded", "failed"])

try:
    runs = api.get_agent_runs(
        campaign_id,
        agent_name=None if agent_filter == "(all)" else agent_filter,
    )
except Exception as e:
    af_note(f"<b>Failed to load runs.</b> {_esc(e)}", tone="bad")
    runs = {"runs": []}

run_list = runs.get("runs", [])
if status_filter != "(all)":
    run_list = [r for r in run_list if str(r.get("status", "")).strip().lower() == status_filter]

m1, m2, m3, m4 = st.columns(4)
with m1:
    af_metric("Total runs", f"{len(run_list):,}")
with m2:
    succeeded = sum(1 for r in run_list if r["status"] == "succeeded")
    af_metric("Succeeded", f"{succeeded:,}")
with m3:
    failed = sum(1 for r in run_list if r["status"] == "failed")
    af_metric("Failed", f"{failed:,}")
with m4:
    total_tokens = sum(r.get("usage", {}).get("total_tokens", 0) for r in run_list)
    af_metric("Total tokens", f"{total_tokens:,}")

total_cost = sum(float((r.get("usage") or {}).get("cost_usd") or 0.0) for r in run_list)
if run_list and total_cost == 0.0:
    af_caption("LLM cost is $0.00 in the demo: Coarena is the free tier.")

# Runs ledger
if run_list:
    run_rows = [
        [
            f'<span class="af-term">{_esc(r["agent_name"])}</span>',
            _run_badge(r["status"]),
            _esc(_fmt_ts(r["started_at"])),
            _fmt_duration(r.get("duration_seconds")),
            f"{int((r.get('usage') or {}).get('total_tokens') or 0):,}",
            f"${float((r.get('usage') or {}).get('cost_usd') or 0.0):,.2f}",
        ]
        for r in run_list
    ]
    st.markdown(
        af_table(
            ["Agent", "Status", "Started", "Duration (s)", "Tokens", "Cost"],
            run_rows,
            numeric_cols={3, 4, 5},
        ),
        unsafe_allow_html=True,
    )

    # Drill-down into a single run
    af_section("Inspect a single run", "Read the exact payload each agent received and returned.")
    selected_idx = st.selectbox(
        "Run",
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
    af_section("Audit log", "The 100 most recent logged events.")
    try:
        events = api.get_audit_events(campaign_id)
        event_rows = [
            [
                _esc(_fmt_ts(e["occurred_at"])),
                f'<span class="af-term">{_esc(e["event_type"])}</span>',
                _esc(e.get("agent_name") or "-"),
                _esc(_summarise(e["payload"])),
            ]
            for e in events.get("events", [])[:100]
        ]
        st.markdown(
            af_table(["Time", "Type", "Agent", "Summary"], event_rows),
            unsafe_allow_html=True,
        )
    except Exception as e:
        af_note(f"<b>Could not load audit events.</b> {_esc(e)}", tone="warn")
else:
    if agent_filter != "(all)" or status_filter != "(all)":
        body = "No runs match the current agent and status filters."
    else:
        body = "Run the campaign from the Campaigns page to see its traces here."
    af_empty("No agent runs", body)
    st.stop()
