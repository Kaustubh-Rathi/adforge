"""ROI Calculator page — the 'was it worth it?' verdict."""
from __future__ import annotations

import plotly.express as px
import streamlit as st

from adforge.dashboard import api_client as api

st.title("ROI Calculator")
st.caption("The bottom line — was the campaign worth it?")

try:
    campaigns = api.list_campaigns()
except Exception as e:
    st.error(f"Failed to load campaigns: {e}")
    st.stop()

if not campaigns:
    st.info("No campaigns yet.")
    st.stop()

campaign_id = st.selectbox(
    "Campaign",
    options=[c["id"] for c in campaigns],
    format_func=lambda cid: next(c["name"] for c in campaigns if c["id"] == cid),
)

try:
    by_channel = api.get_aggregated_kpis(campaign_id, group_by="channel")
except Exception as e:
    st.error(f"Failed to load KPIs: {e}")
    st.stop()

buckets = by_channel.get("buckets", [])
if not buckets:
    st.info("No KPI data yet for this campaign. Run the agent graph first.")
    st.stop()

# Top metrics
totals = {
    "spend": sum(b["spend_usd"] for b in buckets),
    "revenue": sum(b["revenue_usd"] for b in buckets),
    "conversions": sum(b["conversions"] for b in buckets),
    "impressions": sum(b["impressions"] for b in buckets),
    "clicks": sum(b["clicks"] for b in buckets),
}
blended_roas = totals["revenue"] / totals["spend"] if totals["spend"] else 0
blended_cpa = totals["spend"] / totals["conversions"] if totals["conversions"] else 0
blended_ctr = totals["clicks"] / totals["impressions"] if totals["impressions"] else 0
net_profit = totals["revenue"] - totals["spend"]

if blended_roas >= 4.0:
    recommendation, color = "SCALE", "green"
elif blended_roas >= 2.0:
    recommendation, color = "OPTIMIZE", "orange"
elif blended_roas >= 1.0:
    recommendation, color = "PAUSE", "yellow"
else:
    recommendation, color = "KILL", "red"

st.divider()
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("Total Spend", f"${totals['spend']:,.2f}")
with col2:
    st.metric("Total Revenue", f"${totals['revenue']:,.2f}")
with col3:
    st.metric("Net Profit", f"${net_profit:+,.2f}")
with col4:
    st.metric("Blended ROAS", f"{blended_roas:.2f}x")

col1, col2, col3 = st.columns(3)
with col1:
    st.metric("Total Conversions", f"{totals['conversions']:,}")
with col2:
    st.metric("Blended CPA", f"${blended_cpa:.2f}")
with col3:
    st.metric("Blended CTR", f"{blended_ctr*100:.2f}%")

st.divider()
st.subheader(f"Recommendation: :{color}[{recommendation}]")
if recommendation == "SCALE":
    st.success("Campaign is profitable. Increase budget 20-50% on the best-performing channel.")
elif recommendation == "OPTIMIZE":
    st.warning("Campaign is breakeven-to-positive. Reallocate budget from low ROAS channels to top performer.")
elif recommendation == "PAUSE":
    st.warning("Campaign is underwater but recoverable. Pause worst channel, rotate creative, retry.")
else:
    st.error("Campaign is losing money. Kill it and redesign from scratch.")

st.divider()
st.subheader("Per-channel breakdown")

# Bar chart: ROAS by channel
chart_data = []
for b in buckets:
    chart_data.append({
        "Channel": b["bucket"],
        "ROAS": b["roas"],
        "Spend (USD)": b["spend_usd"],
        "Revenue (USD)": b["revenue_usd"],
        "CPA (USD)": b["cpa"],
        "CTR (%)": b["ctr"] * 100,
    })

import pandas as pd
df = pd.DataFrame(chart_data)

col_a, col_b = st.columns(2)
with col_a:
    fig = px.bar(df, x="Channel", y="ROAS", title="ROAS by Channel",
                 color="ROAS", color_continuous_scale="RdYlGn")
    st.plotly_chart(fig, use_container_width=True)
with col_b:
    fig2 = px.bar(df, x="Channel", y=["Spend (USD)", "Revenue (USD)"],
                  title="Spend vs Revenue by Channel", barmode="group")
    st.plotly_chart(fig2, use_container_width=True)

st.dataframe(df, use_container_width=True, hide_index=True)

st.divider()
st.subheader("Time series (last 30 days)")
try:
    kpis = api.get_kpis(campaign_id, days_back=30)
    df_ts = api.kpis_to_df(kpis)
    if not df_ts.empty:
        fig3 = px.line(df_ts, x="captured_at", y="spend_usd", color="channel",
                       title="Daily Spend by Channel")
        st.plotly_chart(fig3, use_container_width=True)
        fig4 = px.line(df_ts, x="captured_at", y="roas", color="channel",
                       title="Daily ROAS by Channel")
        st.plotly_chart(fig4, use_container_width=True)
    else:
        st.info("No time-series data yet.")
except Exception as e:
    st.warning(f"Failed to load time series: {e}")
