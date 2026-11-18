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
    af_caption,
    af_empty,
    af_metric,
    af_note,
    af_section,
    inject_css,
)

inject_css()
api.tenant_switcher()

# Packet documents render as label/value dossiers; the risk badge is the only
# colour these documents use.


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
af_note(
    "<b>Nothing launches without you.</b> Approving resumes the graph and unlocks the "
    "write-side tools; rejecting routes the brief back to the creative strategist with "
    "your comments.",
    tone="info",
)

# Load pending reviews
try:
    pending = api.list_pending_reviews()
except Exception as e:
    af_note(f"<b>Failed to load pending reviews.</b> {html.escape(str(e))}", "bad")
    st.stop()

if not pending.get("pending"):
    af_empty(
        "Queue is clear",
        "No campaign is paused at the human gate. Run a campaign on the Campaigns page "
        "to populate this queue.",
        hint="Campaigns → Run agent graph",
        kicker="0 campaigns awaiting review",
    )
    st.stop()

af_metric("Campaigns awaiting review", f"{pending.get('count', 0):,}")

reviewer_email = st.text_input(
    "Your email (must be in APP_REVIEWER_ALLOWLIST)",
    value=st.session_state.get("reviewer_email", ""),
    placeholder="you@company.com",
    help="This is sent as X-AdForge-Reviewer. Set APP_REVIEWER_ALLOWLIST env var to restrict.",
)
if reviewer_email:
    st.session_state["reviewer_email"] = reviewer_email
if not reviewer_email:
    af_caption(
        "Enter the reviewer email to enable the decision buttons — it is sent as "
        "X-AdForge-Reviewer."
    )

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
    af_note(f"<b>Failed to load review packet.</b> {html.escape(str(e))}", "bad")
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
    af_note("No creative concept in packet.", tone="warn")

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
    total_budget = float(media_plan.get("total_budget_usd", 0))
    rows = (
        _row("Total budget", f"<strong>${total_budget:,.2f}</strong>")
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
            f'<th class="af-right">Expected CVR</th></tr></thead>'
            f"<tbody>{alloc_rows}</tbody></table>",
        )
    st.markdown(_doc("Media plan", rows), unsafe_allow_html=True)
else:
    af_note("No media plan in packet.", tone="warn")

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
    af_note("No compliance verdict in packet.", tone="warn")

# --- Reviewer decision ---
af_section("Your decision", "Approved campaigns resume the graph; rejections return for revision.")
comments = st.text_area("Comments (will be passed to the next creative revision if rejected)",
                        value="", height=100)

col1, col2 = st.columns(2)
with col1:
    if st.button("Approve", type="primary", disabled=not reviewer_email):
        with st.spinner("Approving + resuming graph..."):
            try:
                api.approve_campaign(selected_id, comments, reviewer_email)
                st.toast("Approved — graph resumed", icon="✅")
                st.rerun()
            except Exception as e:
                af_note(f"<b>Approve failed.</b> {html.escape(str(e))}", "bad")

with col2:
    if st.button("Reject", disabled=not reviewer_email):
        if not comments:
            af_note("Reject requires comments — explain what to revise.", tone="warn")
        else:
            with st.spinner("Rejecting + routing back to creative_strategist..."):
                try:
                    api.reject_campaign(selected_id, comments, reviewer_email)
                    st.toast("Rejected — routed to creative_strategist", icon="↩️")
                    st.rerun()
                except Exception as e:
                    af_note(f"<b>Reject failed.</b> {html.escape(str(e))}", "bad")

if not reviewer_email:
    af_caption("Enter your email above to enable Approve and Reject.")

with st.expander("Raw packet (for debugging)"):
    st.json(packet_resp)
