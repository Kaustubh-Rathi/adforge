"""Review Queue page — HITL approval workflow.

Shows campaigns paused at the human_review interrupt. Reviewer can:
  - See the creative concept + media plan + compliance verdict
  - Approve (resumes graph, issues approval token for write MCP tools)
  - Reject (routes graph back to creative_strategist with reviewer comments)
"""
from __future__ import annotations

import json

import streamlit as st

from adforge.dashboard import api_client as api

st.title("Review Queue")
st.caption("Human-in-the-loop approval gate — no creative goes live without your sign-off.")

# Reviewer identity
reviewer_email = st.text_input(
    "Your email (must be in APP_REVIEWER_ALLOWLIST)",
    value=st.session_state.get("reviewer_email", ""),
    help="This is sent as X-AdForge-Reviewer. Set APP_REVIEWER_ALLOWLIST env var to restrict.",
)
if reviewer_email:
    st.session_state["reviewer_email"] = reviewer_email

st.divider()

# Load pending reviews
try:
    pending = api.list_pending_reviews()
except Exception as e:
    st.error(f"Failed to load pending reviews: {e}")
    pending = {"pending": [], "count": 0}

st.metric("Campaigns awaiting review", pending.get("count", 0))

if not pending.get("pending"):
    st.info("No campaigns waiting for review. Run a campaign on the Campaigns page to populate this queue.")
    st.stop()

st.divider()

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

# Render the packet
st.subheader("Creative concept")
creative = packet.get("creative_strategist", {})
if creative:
    st.write(f"**Headline:** {creative.get('headline', '(none)')}")
    st.write(f"**Primary text:** {creative.get('primary_text', '(none)')}")
    st.write(f"**CTA:** {creative.get('cta', '(none)')}")
    st.write(f"**Visual direction:** {creative.get('visual_direction', '(none)')}")
    if creative.get("ab_variants"):
        st.write("**A/B variants:**")
        for v in creative["ab_variants"]:
            st.write(f"  - {v}")
else:
    st.warning("No creative concept in packet.")

st.divider()
st.subheader("Media plan")
media_plan = packet.get("media_planner", {})
if media_plan:
    st.write(f"**Total budget:** ${media_plan.get('total_budget_usd', 0):,.2f}")
    st.write(f"**Bidding strategy:** {media_plan.get('bidding_strategy', 'unknown')}")
    st.write(f"**Flight:** {media_plan.get('flight_dates', {})}")
    st.write("**Channel allocations:**")
    for alloc in media_plan.get("channel_allocations", []):
        st.write(f"  - {alloc.get('channel', '?')}: ${alloc.get('budget_usd', 0):,.2f} "
                 f"(expected CPC ${alloc.get('expected_cpc', 0):.2f}, CVR {alloc.get('expected_cvr', 0)*100:.1f}%)")
else:
    st.warning("No media plan in packet.")

st.divider()
st.subheader("Compliance verdict")
compliance = packet.get("compliance", {})
if compliance:
    approved = compliance.get("approved", False)
    risk = compliance.get("risk_level", "unknown")
    color = "green" if approved and risk == "low" else "orange" if risk in ("medium",) else "red" if risk in ("high",) else "red"
    st.markdown(f"**Compliance:** :{color}[{risk}] — {'APPROVED' if approved else 'NOT APPROVED'}")
    if compliance.get("flagged_issues"):
        st.write("**Flagged issues:**")
        for issue in compliance["flagged_issues"]:
            st.write(f"  - {issue}")
    if compliance.get("required_disclosures"):
        st.write("**Required disclosures:**")
        for d in compliance["required_disclosures"]:
            st.write(f"  - {d}")
    if compliance.get("reviewer_notes"):
        st.write(f"**Reviewer notes:** {compliance['reviewer_notes']}")
else:
    st.warning("No compliance verdict in packet.")

# Reviewer decision
st.divider()
st.subheader("Your decision")
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

st.divider()
with st.expander("Raw packet (for debugging)"):
    st.json(packet_resp)
