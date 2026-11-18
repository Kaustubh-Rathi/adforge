"""ROI Calculator page — the 'was it worth it?' verdict."""
from __future__ import annotations

import html
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from adforge.dashboard import api_client as api
from adforge.dashboard.theme import (
    ACCENT,
    af_caption,
    af_metric,
    af_section,
    inject_css,
    status_color,
)

inject_css()

# The verdict banner is this page's one loud element; everything else stays quiet.
st.markdown(
    """
    <style>
    .af-verdict {
      display: grid; grid-template-columns: minmax(220px, auto) 1fr; gap: 2rem;
      align-items: center; background: var(--af-surface);
      border: 1px solid var(--af-border); border-left: 6px solid var(--af-accent);
      border-radius: var(--af-radius); box-shadow: var(--af-shadow);
      padding: 1.5rem 1.75rem; margin: 0.6rem 0 1.1rem;
    }
    .af-verdict-word { font-size: 2.6rem; font-weight: 800; letter-spacing: -0.03em;
      line-height: 1; }
    .af-verdict-kicker { font-size: 0.8rem; font-weight: 600; color: var(--af-muted);
      margin-bottom: 0.45rem; }
    .af-verdict-stats { display: grid; grid-template-columns: repeat(4, auto); gap: 1.8rem; }
    .af-verdict-stat-label { font-size: 0.76rem; font-weight: 600; color: var(--af-muted); }
    .af-verdict-stat-value { font-size: 1.3rem; font-weight: 700; color: var(--af-heading);
      font-variant-numeric: tabular-nums; margin-top: 0.15rem; letter-spacing: -0.01em; }
    .af-verdict-why { grid-column: 1 / -1; font-size: 0.88rem; color: var(--af-body);
      line-height: 1.55; border-top: 1px solid var(--af-border); padding-top: 0.8rem;
      max-width: 88ch; }
    @media (max-width: 900px) {
      .af-verdict { grid-template-columns: 1fr; gap: 1rem; }
      .af-verdict-stats { grid-template-columns: repeat(2, auto); }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

#: Verdict word -> badge token in the theme's status palette.
_VERDICT_TOKENS = {"SCALE": "approved", "OPTIMIZE": "reviewing", "PAUSE": "blocked", "KILL": "failed"}

_LAYOUT = {
    "template": "plotly_white",
    "margin": {"l": 8, "r": 8, "t": 40, "b": 8},
    "font": {"family": "Inter, sans-serif", "size": 12, "color": "#475569"},
    "plot_bgcolor": "#FFFFFF",
    "paper_bgcolor": "#FFFFFF",
}

af_section("ROI verdict", "The bottom line across every measured channel — was it worth it?")

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
    st.info("No KPI data yet - run the agent graph from the Campaigns page.")
    st.stop()

# Provenance: the API flags any bucket containing simulated rows, so the page
# never presents mock figures as measured ones.
_simulated_buckets = [b for b in buckets if b.get("is_mock")]
_all_simulated = bool(buckets) and all(b.get("all_simulated") for b in buckets)
if _simulated_buckets:
    _channels = ", ".join(sorted(str(b.get("bucket")) for b in _simulated_buckets))
    af_caption(
        f"Simulated data: {_channels} "
        f"{'are' if len(_simulated_buckets) > 1 else 'is'} generated mock KPI rows "
        "(is_mock=true), not platform measurements. "
        "The ROI arithmetic is real; the inputs are not."
        if not _all_simulated
        else f"Simulated data: every channel ({_channels}) uses generated mock KPI "
        "rows (is_mock=true). The ROI arithmetic is real; the inputs are not."
    )

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


def _verdict_rationale() -> str:
    """Latest roi_calculator rationale, if the agent has already run."""
    try:
        runs: list[dict[str, Any]] = api.get_agent_runs(campaign_id, agent_name="roi_calculator").get("runs", [])
    except Exception:
        return ""
    for run in reversed(runs):
        if run.get("status") == "succeeded":
            payload = run.get("output_payload") or {}
            rationale = str(payload.get("rationale") or "").strip()
            if rationale:
                return rationale
    return ""


verdict_color = status_color(_VERDICT_TOKENS[recommendation])
why = _verdict_rationale()
why_html = (
    f'<div class="af-verdict-why"><b style="color:#0F172A">Agent rationale.</b> '
    f'{html.escape(why)}</div>'
    if why
    else ""
)
st.markdown(
    f'<div class="af-verdict" style="border-left-color:{verdict_color}">'
    f'<div>'
    f'<div class="af-verdict-kicker">Recommended action</div>'
    f'<div class="af-verdict-word" style="color:{verdict_color}">{recommendation}</div>'
    f'</div>'
    f'<div class="af-verdict-stats">'
    f'<div><div class="af-verdict-stat-label">Blended ROAS</div>'
    f'<div class="af-verdict-stat-value">{blended_roas:.2f}x</div></div>'
    f'<div><div class="af-verdict-stat-label">Net profit</div>'
    f'<div class="af-verdict-stat-value" style="color:'
    f'{status_color("approved") if net_profit >= 0 else status_color("failed")}">'
    f'${net_profit:+,.2f}</div></div>'
    f'<div><div class="af-verdict-stat-label">Spend</div>'
    f'<div class="af-verdict-stat-value">${totals["spend"]:,.2f}</div></div>'
    f'<div><div class="af-verdict-stat-label">Revenue</div>'
    f'<div class="af-verdict-stat-value">${totals["revenue"]:,.2f}</div></div>'
    f'</div>'
    f'{why_html}'
    f'</div>',
    unsafe_allow_html=True,
)

m1, m2, m3, m4 = st.columns(4)
with m1:
    af_metric("Total spend", f"${totals['spend']:,.2f}")
with m2:
    af_metric("Total revenue", f"${totals['revenue']:,.2f}")
with m3:
    af_metric("Net profit", f"${net_profit:+,.2f}")
with m4:
    af_metric("Blended ROAS", f"{blended_roas:.2f}x")

m5, m6, m7 = st.columns(3)
with m5:
    af_metric("Total conversions", f"{totals['conversions']:,}")
with m6:
    af_metric("Blended CPA", f"${blended_cpa:.2f}")
with m7:
    af_metric("Blended CTR", f"{blended_ctr*100:.2f}%")

if recommendation == "SCALE":
    st.success("Campaign is profitable. Increase budget 20-50% on the best-performing channel.")
elif recommendation == "OPTIMIZE":
    st.warning("Campaign is breakeven-to-positive. Reallocate budget from low ROAS channels to top performer.")
elif recommendation == "PAUSE":
    st.warning("Campaign is underwater but recoverable. Pause worst channel, rotate creative, retry.")
else:
    st.error("Campaign is losing money. Kill it and redesign from scratch.")

af_section("Per-channel breakdown")

chart_data = []
for b in buckets:
    chart_data.append({
        "Channel": b["bucket"],
        "ROAS": b["roas"],
        "Spend (USD)": b["spend_usd"],
        "Revenue (USD)": b["revenue_usd"],
        "CPA (USD)": b["cpa"],
        "CTR (%)": b["ctr"] * 100,
        "Data": "Simulated" if b.get("is_mock") else "Measured",
    })

df = pd.DataFrame(chart_data)


def _roas_verdict(roas: float) -> str:
    if roas >= 4.0:
        return "Scale"
    if roas >= 2.0:
        return "Optimize"
    if roas >= 1.0:
        return "Pause"
    return "Kill"


df["Verdict"] = df["ROAS"].apply(_roas_verdict)
_verdict_colors = {
    "Scale": status_color("approved"),
    "Optimize": status_color("reviewing"),
    "Pause": status_color("blocked"),
    "Kill": status_color("failed"),
}

col_a, col_b = st.columns(2)
with col_a:
    fig = px.bar(df, x="Channel", y="ROAS", color="Verdict", title="ROAS by channel",
                 color_discrete_map=_verdict_colors)
    fig.update_layout(showlegend=False, **_LAYOUT)
    fig.add_hline(y=4.0, line_dash="dot", line_color="#94A3B8", annotation_text="scale at 4x")
    st.plotly_chart(fig, use_container_width=True)
with col_b:
    fig2 = px.bar(df, x="Channel", y=["Spend (USD)", "Revenue (USD)"],
                  title="Spend vs revenue by channel", barmode="group",
                  color_discrete_map={"Spend (USD)": "#94A3B8", "Revenue (USD)": ACCENT})
    fig2.update_layout(**_LAYOUT)
    st.plotly_chart(fig2, use_container_width=True)

st.dataframe(df, use_container_width=True, hide_index=True)

af_section("Time series", "Daily spend and ROAS per channel, last 30 days.")
try:
    kpis = api.get_kpis(campaign_id, days_back=30)
    df_ts = api.kpis_to_df(kpis)
    if not df_ts.empty:
        fig3 = px.line(df_ts, x="captured_at", y="spend_usd", color="channel",
                       title="Daily spend by channel")
        fig3.update_layout(**_LAYOUT)
        st.plotly_chart(fig3, use_container_width=True)
        fig4 = px.line(df_ts, x="captured_at", y="roas", color="channel",
                       title="Daily ROAS by channel")
        fig4.update_layout(**_LAYOUT)
        st.plotly_chart(fig4, use_container_width=True)
    else:
        st.info("No time-series data yet.")
except Exception as e:
    st.warning(f"Failed to load time series: {e}")
