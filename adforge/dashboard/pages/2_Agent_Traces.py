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
    af_metric,
    af_section,
    inject_css,
)

inject_css()
api.tenant_switcher()

st.markdown(
    """
    <style>
    .af-table-wrap {
      background: var(--af-surface); border: 1px solid var(--af-border);
      border-radius: var(--af-radius); box-shadow: var(--af-shadow); overflow: hidden;
      margin-top: 0.4rem;
    }
    table.af-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    table.af-table th {
      text-align: left; font-weight: 600; color: var(--af-heading);
      font-size: 0.78rem; padding: 0.6rem 1rem; background: var(--af-canvas);
      border-bottom: 1px solid var(--af-border);
    }
    table.af-table td {
      padding: 0.55rem 1rem; border-bottom: 1px solid var(--af-border);
      color: var(--af-body); font-variant-numeric: tabular-nums;
    }
    table.af-table tr:last-child td { border-bottom: none; }
    table.af-table td.af-agent { font-weight: 600; color: var(--af-heading); }
    table.af-table td.af-num { text-align: right; }
    </style>
    """,
    unsafe_allow_html=True,
)

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


def _summarise(payload: dict) -> str:
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
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime(
            "%d %b %Y, %H:%M:%S"
        )
    except ValueError:
        return text


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

run_list = runs.get("runs", [])

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

# Runs ledger
if run_list:
    run_rows = "".join(
        '<tr>'
        f'<td class="af-agent">{_esc(r["agent_name"])}</td>'
        f'<td>{_run_badge(r["status"])}</td>'
        f'<td>{_esc(_fmt_ts(r["started_at"]))}</td>'
        f'<td class="af-num">{(r.get("duration_seconds") or 0.0):.1f}</td>'
        f'<td class="af-num">{int((r.get("usage") or {}).get("total_tokens") or 0):,}</td>'
        f'<td class="af-num">${float((r.get("usage") or {}).get("cost_usd") or 0.0):.4f}</td>'
        '</tr>'
        for r in run_list
    )
    st.markdown(
        '<div class="af-table-wrap"><table class="af-table">'
        "<thead><tr><th>Agent</th><th>Status</th><th>Started</th>"
        '<th>Duration (s)</th><th style="text-align:right">Tokens</th>'
        '<th style="text-align:right">Cost</th></tr></thead>'
        f"<tbody>{run_rows}</tbody></table></div>",
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
        event_rows = "".join(
            '<tr>'
            f'<td>{_esc(_fmt_ts(e["occurred_at"]))}</td>'
            f'<td class="af-agent">{_esc(e["event_type"])}</td>'
            f'<td>{_esc(e.get("agent_name") or "-")}</td>'
            f'<td>{_esc(_summarise(e["payload"]))}</td>'
            '</tr>'
            for e in events.get("events", [])[:100]
        )
        st.markdown(
            '<div class="af-table-wrap"><table class="af-table">'
            "<thead><tr><th>Time</th><th>Type</th><th>Agent</th>"
            f"<th>Summary</th></tr></thead><tbody>{event_rows}</tbody></table></div>",
            unsafe_allow_html=True,
        )
    except Exception as e:
        st.warning(f"Could not load audit events: {e}")
else:
    st.info("No agent runs yet. Run a campaign from the Campaigns page.")
