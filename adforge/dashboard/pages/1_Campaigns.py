"""AdForge dashboard — Campaigns overview + create + run."""
from __future__ import annotations

import html
from typing import Any

import streamlit as st

from adforge.dashboard import api_client as api
from adforge.dashboard.theme import (
    af_badge,
    af_caption,
    af_metric,
    af_section,
    inject_css,
    status_color,
)

inject_css()
api.tenant_switcher()

# Page-local styles for the campaign ledger rows (the theme owns everything else).
st.markdown(
    """
    <style>
    .af-ops-list { display: flex; flex-direction: column; gap: 0.55rem; margin-top: 0.3rem; }
    .af-ops-row {
      display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 1.25rem;
      background: var(--af-surface); border: 1px solid var(--af-border);
      border-left: 3px solid var(--af-accent); border-radius: 10px;
      box-shadow: var(--af-shadow); padding: 0.75rem 1.1rem;
    }
    .af-ops-name { font-weight: 600; color: var(--af-heading); font-size: 0.92rem; }
    .af-ops-meta { display: flex; align-items: center; gap: 0.6rem; margin-top: 0.3rem; }
    .af-ops-id { color: var(--af-muted); font-size: 0.74rem; }
    .af-ops-figures { display: flex; gap: 1.7rem; text-align: right; }
    .af-ops-fig-label { font-size: 0.72rem; font-weight: 600; color: var(--af-muted); }
    .af-ops-fig-value {
      font-size: 0.98rem; font-weight: 700; color: var(--af-heading);
      font-variant-numeric: tabular-nums; margin-top: 0.1rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

_REVIEW_STATUSES = {"reviewing", "pending", "awaiting_review", "in_review"}

af_section("Campaigns", "Create briefs, run the seven-agent graph, track spend.")
af_caption(f"Tenant: {api.current_tenant()}")

try:
    campaigns: list[dict[str, Any]] = api.list_campaigns()
except Exception as e:
    st.error(f"Failed to load campaigns: {e}")
    campaigns = []

m1, m2, m3 = st.columns(3)
with m1:
    af_metric("Total campaigns", f"{len(campaigns):,}")
with m2:
    reviewing = sum(1 for c in campaigns if str(c.get("status", "")).lower() in _REVIEW_STATUSES)
    af_metric("Awaiting review", f"{reviewing:,}")
with m3:
    total_spend = sum(float(c.get("total_spend_usd") or 0.0) for c in campaigns)
    af_metric("Platform spend", f"${total_spend:,.2f}")


def _usd(value: Any) -> str:
    return f"${float(value or 0.0):,.2f}"


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
        f'<div>'
        f'<div class="af-ops-name">{html.escape(str(c.get("name", "(unnamed)")))}</div>'
        f'<div class="af-ops-meta">{af_badge(status)}<span class="af-ops-id">{short_id}</span>'
        f'{created_html}</div>'
        f'</div>'
        f'<div class="af-ops-figures">'
        f'<div><div class="af-ops-fig-label">Spend</div>'
        f'<div class="af-ops-fig-value">{_usd(c.get("total_spend_usd"))}</div></div>'
        f'<div><div class="af-ops-fig-label">Revenue</div>'
        f'<div class="af-ops-fig-value">{_usd(c.get("total_revenue_usd"))}</div></div>'
        f'</div>'
        f'</div>'
    )


# --- Create new campaign ---
with st.expander("Create new campaign", expanded=False), st.form("create_campaign_form"):
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
            st.error(f"Failed to create: {e}")

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
    if st.button("Run agent graph", type="primary"):
        with st.spinner("Running 7-agent graph... this can take 1-3 minutes."):
            try:
                result = api.run_campaign(selected_id)
                st.success("Graph completed!")
                st.json(result.get("final_state", {}))
            except Exception as e:
                st.error(f"Run failed: {e}")
else:
    st.info("No campaigns yet. Create one above, or run `make seed` to load the 3 example campaigns.")
