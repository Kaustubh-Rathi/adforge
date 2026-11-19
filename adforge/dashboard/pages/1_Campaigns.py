"""AdForge dashboard — Campaigns overview + create + run."""

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
    status_color,
)

inject_css()
api.tenant_switcher()

_REVIEW_STATUSES = {"reviewing", "pending", "awaiting_review", "in_review"}

af_section("Campaigns", "Create briefs, run the seven-agent graph, track spend.")
af_caption(f"Tenant: {api.current_tenant()}")

try:
    campaigns: list[dict[str, Any]] = api.list_campaigns()
except Exception as e:
    af_note(f"<b>Failed to load campaigns.</b> {html.escape(str(e))}", tone="bad")
    campaigns = []

m1, m2, m3, m4 = st.columns(4)
with m1:
    af_metric("Total campaigns", f"{len(campaigns):,}")
with m2:
    reviewing = sum(1 for c in campaigns if str(c.get("status", "")).lower() in _REVIEW_STATUSES)
    af_metric("Awaiting review", f"{reviewing:,}")
with m3:
    approved = sum(1 for c in campaigns if str(c.get("status", "")).strip().lower() == "approved")
    af_metric("Approved", f"{approved:,}")
with m4:
    total_spend = sum(float(c.get("total_spend_usd") or 0.0) for c in campaigns)
    af_metric("Platform spend", f"${total_spend:,.2f}")


def _usd(value: Any) -> str:
    return f"${float(value or 0.0):,.2f}"


def _campaign_action(status: str) -> str:
    """Link the row to the next useful page for that status."""
    if status.strip().lower() in _REVIEW_STATUSES:
        return '<a class="af-ops-link" href="/Review_Queue" target="_self">Open review &rarr;</a>'
    return '<a class="af-ops-link" href="/ROI_Calculator" target="_self">See ROI &rarr;</a>'


def _campaign_row(c: dict[str, Any]) -> str:
    """One ledger-style row: name and status on the left, money on the right."""
    status = str(c.get("status", ""))
    created = str(c.get("created_at") or c.get("started_at") or "")[:10]
    created_html = (
        f'<span class="af-ops-id">Started {html.escape(created)}</span>' if created else ""
    )
    short_id = html.escape(str(c.get("id", ""))[:8])
    return (
        f'<div class="af-ops-row" style="border-left-color:{status_color(status)}">'
        f"<div>"
        f'<div class="af-ops-name">{html.escape(str(c.get("name", "(unnamed)")))}</div>'
        f'<div class="af-ops-meta">{af_badge(status)}'
        f'<span class="af-ops-id">id {short_id}</span>'
        f"{created_html}{_campaign_action(status)}</div>"
        f"</div>"
        f'<div class="af-ops-figures">'
        f'<div><div class="af-ops-fig-label">Spend</div>'
        f'<div class="af-ops-fig-value">{_usd(c.get("total_spend_usd"))}</div></div>'
        f'<div><div class="af-ops-fig-label">Revenue</div>'
        f'<div class="af-ops-fig-value">{_usd(c.get("total_revenue_usd"))}</div></div>'
        f"</div>"
        f"</div>"
    )


# --- Create new campaign ---
with st.expander("Create new campaign", expanded=False), st.form("create_campaign_form"):
    af_caption("The graph runs on the free Coarena LLM — no paid API key is required.")
    name = st.text_input("Campaign name", value="")
    product_name = st.text_input("Product name")
    product_description = st.text_area("Product description")
    target_audience = st.text_area("Target audience")
    col_budget, col_duration = st.columns(2)
    with col_budget:
        budget_usd = st.number_input(
            "Budget (USD)", min_value=100.0, max_value=1_000_000.0, value=10000.0, step=500.0
        )
    with col_duration:
        duration_days = st.slider("Duration (days)", min_value=1, max_value=90, value=30)
    business_objective = st.selectbox(
        "Objective", ["awareness", "consideration", "conversion", "retention"]
    )
    channels = st.multiselect(
        "Channels", ["google", "meta", "linkedin", "tiktok"], default=["google", "meta"]
    )

    submitted = st.form_submit_button("Create campaign")
    if submitted and name and product_name and product_description:
        brief = {
            "product_name": product_name,
            "product_description": product_description,
            "target_audience": target_audience,
            "budget_usd": budget_usd,
            "duration_days": duration_days,
            "business_objective": business_objective,
            "channels_hint": channels,
            "constraints": [],
        }
        try:
            new_campaign = api.create_campaign(name, brief)
            st.success(f"Created campaign: {new_campaign['id']}")
            st.rerun()
        except Exception as e:
            af_note(f"<b>Failed to create campaign.</b> {html.escape(str(e))}", tone="bad")

# --- Campaign ledger ---
if campaigns:
    st.markdown(
        '<div class="af-ops-list">' + "".join(_campaign_row(c) for c in campaigns) + "</div>",
        unsafe_allow_html=True,
    )

    af_section("Run the agent graph", "Executes all seven agents and updates the campaign status.")
    selected_id = st.selectbox(
        "Campaign",
        options=[c["id"] for c in campaigns],
        format_func=lambda cid: next(c["name"] for c in campaigns if c["id"] == cid),
    )
    run_clicked = st.button("Run agent graph", type="primary")
    af_caption("Runs the full graph; usually 1–3 minutes on the free LLM.")
    if run_clicked:
        with st.spinner("Running 7-agent graph... this can take 1-3 minutes."):
            try:
                result = api.run_campaign(selected_id)
                af_note("<b>Graph completed.</b> Campaign status and traces are updated.")
                with st.expander("Final graph state", expanded=False):
                    st.json(result.get("final_state", {}))
            except Exception as e:
                af_note(f"<b>Run failed.</b> {html.escape(str(e))}", tone="bad")
else:
    af_empty(
        "No campaigns in this tenant",
        "Create a brief above, or load the three seeded demo campaigns.",
        hint="make seed",
    )
    st.stop()
