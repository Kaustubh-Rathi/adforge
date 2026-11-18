"""Review Queue page — HITL approval workflow.

Shows campaigns paused at the human_review interrupt. Reviewer can:
  - See the creative concept + media plan + compliance verdict
  - Approve (resumes graph, issues approval token for write MCP tools)
  - Reject (routes graph back to creative_strategist with reviewer comments)
"""
from __future__ import annotations

import html
from typing import Any

import streamlit as st

from adforge.dashboard import api_client as api
from adforge.dashboard.theme import (
    af_badge,
    af_card,
    af_metric,
    af_section,
    inject_css,
)

inject_css()

# Packet documents render as label/value dossiers; the risk accent stripe is
# the only colour these cards are allowed to use.
st.markdown(
    """
    <style>
    .af-doc {
      background: var(--af-surface); border: 1px solid var(--af-border);
      border-radius: var(--af-radius); box-shadow: var(--af-shadow);
      padding: 1.1rem 1.3rem; margin-bottom: 0.9rem;
    }
    .af-doc-head {
      display: flex; justify-content: space-between; align-items: baseline; gap: 1rem;
      border-bottom: 1px solid var(--af-border); padding-bottom: 0.55rem; margin-bottom: 0.75rem;
    }
    .af-doc-title { font-weight: 700; color: var(--af-heading); font-size: 1rem; }
    .af-doc-row {
      display: grid; grid-template-columns: 170px minmax(0, 1fr);
      gap: 1rem; padding: 0.32rem 0; font-size: 0.88rem;
    }
    .af-doc-label { color: var(--af-muted); font-weight: 600; font-size: 0.8rem; }
    .af-doc-value { color: var(--af-body); line-height: 1.5; word-break: break-word; }
    .af-doc-value strong { color: var(--af-heading); font-weight: 600; }
    .af-doc-list { margin: 0.15rem 0 0 1.1rem; padding: 0; }
    .af-doc-list li { font-size: 0.86rem; color: var(--af-body); margin: 0.15rem 0; }
    .af-alloc-table { width: 100%; border-collapse: collapse; font-size: 0.84rem; margin-top: 0.3rem; }
    .af-alloc-table th {
      text-align: left; font-weight: 600; font-size: 0.78rem; color: var(--af-heading);
      border-bottom: 1px solid var(--af-border); padding: 0.35rem 0.6rem 0.35rem 0;
    }
    .af-alloc-table td {
      padding: 0.35rem 0.6rem 0.35rem 0; border-bottom: 1px solid var(--af-border);
      color: var(--af-body); font-variant-numeric: tabular-nums;
    }
    .af-alloc-table tr:last-child td { border-bottom: none; }
    .af-alloc-table td.af-right, .af-alloc-table th.af-right { text-align: right; }
    @media (max-width: 760px) {
      .af-doc-row { grid-template-columns: 1fr; gap: 0.15rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _doc(title: str, rows_html: str, head_badge: str = "") -> str:
    badge_html = f'<div>{head_badge}</div>' if head_badge else ""
    return (
        '<div class="af-doc">'
        f'<div class="af-doc-head"><div class="af-doc-title">{_esc(title)}</div>{badge_html}</div>'
        f"{rows_html}</div>"
    )


def _row(label: str, value_html: str) -> str:
    return (
        '<div class="af-doc-row">'
        f'<div class="af-doc-label">{_esc(label)}</div>'
        f'<div class="af-doc-value">{value_html}</div>'
        "</div>"
    )


def _bullets(items: list[Any]) -> str:
    return '<ul class="af-doc-list">' + "".join(f"<li>{_esc(i)}</li>" for i in items) + "</ul>"


def _packet_section(packet: dict[str, Any], *keys: str) -> dict[str, Any]:
    """First non-empty packet section under any of its historical key names."""
    for key in keys:
        value = packet.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


# --- Header + HITL note ---
af_section("Review queue", "Campaigns paused at the human gate, waiting for your decision.")
st.markdown(
    af_card(
        "",
        "Nothing launches without you",
        "Approving resumes the graph and unlocks the write-side tools; rejecting routes the "
        "brief back to the creative strategist with your comments.",
    ),
    unsafe_allow_html=True,
)

reviewer_email = st.text_input(
    "Your email (must be in APP_REVIEWER_ALLOWLIST)",
    value=st.session_state.get("reviewer_email", ""),
    help="This is sent as X-AdForge-Reviewer. Set APP_REVIEWER_ALLOWLIST env var to restrict.",
)
if reviewer_email:
    st.session_state["reviewer_email"] = reviewer_email

# Load pending reviews
try:
    pending = api.list_pending_reviews()
except Exception as e:
    st.error(f"Failed to load pending reviews: {e}")
    pending = {"pending": [], "count": 0}

af_metric("Campaigns awaiting review", f"{pending.get('count', 0):,}")

if not pending.get("pending"):
    st.info("No campaigns waiting for review. Run a campaign on the Campaigns page to populate this queue.")
    st.stop()

# Reviewer picker — select which campaign to review
campaigns = pending["pending"]
selected_id = st.selectbox(
    "Select campaign to review",
    options=[c["id"] for c in campaigns],
    format_func=lambda cid: next(c["name"] for c in campaigns if c["id"] == cid),
)

# Fetch the review packet for the selected campaign
try:
    packet_resp = api.get_review_packet(selected_id)
    packet = packet_resp.get("packet", {})
except Exception as e:
    st.error(f"Failed to load review packet: {e}")
    st.stop()

# --- Creative concept ---
creative = _packet_section(packet, "creative", "creative_strategist")
if creative:
    rows = (
        _row("Headline", f'<strong>{_esc(creative.get("headline", "(none)"))}</strong>')
        + _row("Primary text", _esc(creative.get("primary_text", "(none)")))
        + _row("Call to action", _esc(creative.get("cta", "(none)")))
        + _row("Visual direction", _esc(creative.get("visual_direction", "(none)")))
    )
    if creative.get("ab_variants"):
        rows += _row("A/B variants", _bullets(creative["ab_variants"]))
    st.markdown(_doc("Creative concept", rows), unsafe_allow_html=True)
else:
    st.warning("No creative concept in packet.")

# --- Media plan ---
media_plan = _packet_section(packet, "media_plan", "media_planner")
if media_plan:
    flight = media_plan.get("flight_dates") or {}
    if isinstance(flight, dict) and flight:
        flight_text = (
            f'{_esc(flight.get("start", flight.get("start_date", "?")))} to '
            f'{_esc(flight.get("end", flight.get("end_date", "?")))}'
        )
    else:
        flight_text = _esc(flight) if flight else "unknown"
    rows = (
        _row("Total budget", f'<strong>${float(media_plan.get("total_budget_usd", 0)):,.2f}</strong>')
        + _row("Bidding strategy", _esc(media_plan.get("bidding_strategy", "unknown")))
        + _row("Flight", flight_text)
    )
    allocs = media_plan.get("channel_allocations", [])
    if allocs:
        alloc_rows = "".join(
            "<tr>"
            f'<td>{_esc(a.get("channel", "?"))}</td>'
            f'<td class="af-right">${float(a.get("budget_usd", 0)):,.2f}</td>'
            f'<td class="af-right">${float(a.get("expected_cpc", 0)):.2f}</td>'
            f'<td class="af-right">{float(a.get("expected_cvr", 0)) * 100:.1f}%</td>'
            "</tr>"
            for a in allocs
        )
        rows += _row(
            "Channel allocations",
            '<table class="af-alloc-table"><thead><tr><th>Channel</th>'
            '<th class="af-right">Budget</th><th class="af-right">Expected CPC</th>'
            f'<th class="af-right">Expected CVR</th></tr></thead><tbody>{alloc_rows}</tbody></table>',
        )
    st.markdown(_doc("Media plan", rows), unsafe_allow_html=True)
else:
    st.warning("No media plan in packet.")

# --- Compliance verdict ---
compliance = _packet_section(packet, "compliance_verdict", "compliance")
if compliance:
    approved = compliance.get("approved", False)
    risk = compliance.get("risk_level", "unknown")
    if approved and risk == "low":
        risk_token = "approved"
    elif risk == "medium":
        risk_token = "reviewing"
    else:
        risk_token = "blocked"
    verdict_badge = af_badge(
        risk_token, f"{str(risk).title()} risk, {'approved' if approved else 'not approved'}"
    )
    rows = _row("Reviewer notes", _esc(compliance.get("reviewer_notes", "(none)")))
    if compliance.get("flagged_issues"):
        rows += _row("Flagged issues", _bullets(compliance["flagged_issues"]))
    if compliance.get("required_disclosures"):
        rows += _row("Required disclosures", _bullets(compliance["required_disclosures"]))
    st.markdown(_doc("Compliance verdict", rows, head_badge=verdict_badge), unsafe_allow_html=True)
else:
    st.warning("No compliance verdict in packet.")

# --- Reviewer decision ---
af_section("Your decision", "Approved campaigns resume the graph; rejections return for revision.")
comments = st.text_area("Comments (will be passed to the next creative revision if rejected)",
                        value="", height=100)

col1, col2 = st.columns(2)
with col1:
    if st.button("Approve", type="primary", disabled=not reviewer_email):
        if not reviewer_email:
            st.error("Enter your email above first.")
        else:
            with st.spinner("Approving + resuming graph..."):
                try:
                    result = api.approve_campaign(selected_id, comments, reviewer_email)
                    st.success(f"Approved. Final state keys: {result.get('final_state_keys', [])}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Approve failed: {e}")

with col2:
    if st.button("Reject", disabled=not reviewer_email):
        if not reviewer_email:
            st.error("Enter your email above first.")
        elif not comments:
            st.error("Reject requires comments — explain what to revise.")
        else:
            with st.spinner("Rejecting + routing back to creative_strategist..."):
                try:
                    result = api.reject_campaign(selected_id, comments, reviewer_email)
                    st.success(f"Rejected. Routed to: {result.get('routed_to', 'unknown')}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Reject failed: {e}")

with st.expander("Raw packet (for debugging)"):
    st.json(packet_resp)
