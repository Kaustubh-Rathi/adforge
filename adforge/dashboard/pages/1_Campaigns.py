"""AdForge dashboard — Campaigns overview + create + run."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from adforge.dashboard import api_client as api


st.title("Campaigns")

col1, col2, col3 = st.columns(3)
try:
    campaigns = api.list_campaigns()
except Exception as e:
    st.error(f"Failed to load campaigns: {e}")
    campaigns = []

with col1:
    st.metric("Total Campaigns", len(campaigns))
with col2:
    live = sum(1 for c in campaigns if c["status"] == "live")
    st.metric("Live", live)
with col3:
    total_spend = sum(c["total_spend_usd"] for c in campaigns)
    st.metric("Total Spend", f"${total_spend:,.2f}")

st.divider()

# --- Create new campaign ---
with st.expander("Create new campaign", expanded=False):
    with st.form("create_campaign_form"):
        name = st.text_input("Campaign name", value="")
        product_name = st.text_input("Product name")
        product_description = st.text_area("Product description")
        target_audience = st.text_area("Target audience")
        budget_usd = st.number_input("Budget (USD)", min_value=100.0, max_value=1_000_000.0, value=10000.0, step=500.0)
        duration_days = st.slider("Duration (days)", min_value=1, max_value=90, value=30)
        business_objective = st.selectbox("Objective", ["awareness", "consideration", "conversion", "retention"])
        channels = st.multiselect("Channels", ["google", "meta", "linkedin", "tiktok"], default=["google", "meta"])

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

# --- Campaign table ---
if campaigns:
    df = pd.DataFrame(campaigns)
    df = df[["id", "name", "status", "total_spend_usd", "total_revenue_usd"]]
    df["total_spend_usd"] = df["total_spend_usd"].map("${:,.2f}".format)
    df["total_revenue_usd"] = df["total_revenue_usd"].map("${:,.2f}".format)
    df.columns = ["ID", "Name", "Status", "Spend", "Revenue"]
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.divider()
    st.subheader("Run a campaign through the agent graph")
    selected_id = st.selectbox(
        "Select campaign",
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
