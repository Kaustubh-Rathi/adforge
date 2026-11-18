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
    af_empty,
    af_metric,
    af_note,
    af_section,
    af_verdict,
    inject_css,
    status_color,
)

inject_css()
api.tenant_switcher()

#: Verdict word -> badge token in the theme's status palette.
_VERDICT_TOKENS = {
    "SCALE": "approved",
    "OPTIMIZE": "reviewing",
    "PAUSE": "blocked",
    "KILL": "failed",
}

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
    af_note(f"<b>Failed to load campaigns.</b> {html.escape(str(e))}", "bad")
    st.stop()

if not campaigns:
    af_empty(
        "No campaigns yet",
        "Nothing has been briefed yet. Create a campaign to run the agent graph and "
        "generate ROI data.",
        hint="Campaigns → New campaign",
        kicker="0 campaigns",
    )
    st.stop()

campaign_id = st.selectbox(
    "Campaign",
    options=[c["id"] for c in campaigns],
    format_func=lambda cid: next(c["name"] for c in campaigns if c["id"] == cid),
)

# Daily snapshots feed both the verdict's coverage line and the time-series
# charts at the bottom, so fetch them once, right after the campaign is chosen.
_ts_error: str | None = None
try:
    kpis = api.get_kpis(campaign_id, days_back=30)
    df_ts = api.kpis_to_df(kpis)
except Exception as e:
    df_ts = pd.DataFrame()
    _ts_error = str(e)

try:
    by_channel = api.get_aggregated_kpis(campaign_id, group_by="channel")
except Exception as e:
    af_note(f"<b>Failed to load KPIs.</b> {html.escape(str(e))}", "bad")
    st.stop()

buckets = by_channel.get("buckets", [])
if not buckets:
    af_empty(
        "No KPI data yet",
        "This campaign has no measured or simulated KPI rows, so there is no verdict to "
        "compute. Run the agent graph to generate them.",
        hint="Campaigns → Run agent graph",
        kicker="0 KPI rows",
    )
    st.stop()

# Provenance: the API flags any bucket containing simulated rows, so the page
# never presents mock figures as measured ones.
_simulated_buckets = [b for b in buckets if b.get("is_mock")]
_all_simulated = bool(buckets) and all(b.get("all_simulated") for b in buckets)
if _simulated_buckets:
    _channels = ", ".join(sorted(str(b.get("bucket")) for b in _simulated_buckets))
    af_note(
        f"Simulated data: {_channels} "
        f"{'are' if len(_simulated_buckets) > 1 else 'is'} generated mock KPI rows "
        "(is_mock=true), not platform measurements. "
        "The ROI arithmetic is real; the inputs are not."
        if not _all_simulated
        else f"Simulated data: every channel ({_channels}) uses generated mock KPI "
        "rows (is_mock=true). The ROI arithmetic is real; the inputs are not.",
        tone="warn",
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
    recommendation = "SCALE"
elif blended_roas >= 2.0:
    recommendation = "OPTIMIZE"
elif blended_roas >= 1.0:
    recommendation = "PAUSE"
else:
    recommendation = "KILL"


def _verdict_rationale() -> str:
    """Latest roi_calculator rationale, if the agent has already run."""
    try:
        runs: list[dict[str, Any]] = api.get_agent_runs(
            campaign_id, agent_name="roi_calculator"
        ).get("runs", [])
    except Exception:
        return ""
    for run in reversed(runs):
        if run.get("status") == "succeeded":
            payload = run.get("output_payload") or {}
            rationale = str(payload.get("rationale") or "").strip()
            if rationale:
                return rationale
    return ""


def _coverage_line(df: pd.DataFrame) -> str:
    """One sentence describing the KPI rows the verdict was computed from."""
    stamps = pd.to_datetime(df["captured_at"])
    first, last = stamps.min(), stamps.max()
    if first == last:
        span = f"{first:%d %b %Y}"
    elif first.year == last.year:
        span = f"{first:%d %b} – {last:%d %b %Y}"
    else:
        span = f"{first:%d %b %Y} – {last:%d %b %Y}"
    return f"Verdict computed from {len(df):,} daily KPI rows ({span})."


def _money(value: float) -> str:
    """USD with the sign before the symbol (``-$1,951.67``, not ``$-1,951.67``)."""
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


_CHANNEL_LABELS = {
    "google": "Google",
    "linkedin": "LinkedIn",
    "meta": "Meta",
    "tiktok": "TikTok",
    "bing": "Bing",
}


def _channel(name: object) -> str:
    """Human-facing channel name for prose (``linkedin`` -> ``LinkedIn``)."""
    key = str(name or "").strip().lower()
    return _CHANNEL_LABELS.get(key, str(name or "").strip().title() or "unknown")


def _computed_rationale() -> str:
    """Explain today's verdict from the live KPI buckets.

    The agent's stored rationale is deliberately not used here: it was written
    at the last graph run and may predate the KPI rows the page is showing
    (seeded rows in particular). It stays available in the expander below.
    """
    best = max(buckets, key=lambda b: float(b.get("roas") or 0.0))
    worst = min(buckets, key=lambda b: float(b.get("roas") or 0.0))
    channel_note = ""
    if len(buckets) > 1:
        channel_note = (
            f" {_channel(worst['bucket'])} is the biggest drag at {worst['roas']:.2f}x; "
            f"{_channel(best['bucket'])} returns {best['roas']:.2f}x."
        )
    head = (
        f"Blended ROAS {blended_roas:.2f}x returns {_money(totals['revenue'])} of revenue "
        f"on {_money(totals['spend'])} of spend, a net profit of {_money(net_profit)}."
        if net_profit >= 0
        else f"Blended ROAS {blended_roas:.2f}x is below the 1.0x break-even: "
        f"{_money(totals['revenue'])} of revenue on {_money(totals['spend'])} of spend, "
        f"a net loss of {_money(abs(net_profit))}."
    )
    return head + channel_note


_agent_why = _verdict_rationale()
if not df_ts.empty and "captured_at" in df_ts.columns:
    af_caption(_coverage_line(df_ts))

af_verdict(
    recommendation,
    _VERDICT_TOKENS[recommendation],
    [
        ("Blended ROAS", f"{blended_roas:.2f}x"),
        ("Net profit", _money(net_profit)),
        ("Spend", _money(totals["spend"])),
        ("Revenue", _money(totals["revenue"])),
    ],
    note=_computed_rationale(),
    note_label="Why this verdict.",
)

m1, m2, m3, m4 = st.columns(4)
with m1:
    af_metric("Total spend", _money(totals["spend"]))
with m2:
    af_metric("Total revenue", _money(totals["revenue"]))
with m3:
    af_metric("Net profit", _money(net_profit))
with m4:
    af_metric("Blended ROAS", f"{blended_roas:.2f}x")

m5, m6, m7 = st.columns(3)
with m5:
    af_metric("Total conversions", f"{totals['conversions']:,}")
with m6:
    af_metric("Blended CPA", f"${blended_cpa:.2f}")
with m7:
    af_metric("Blended CTR", f"{blended_ctr * 100:.2f}%")

if recommendation == "SCALE":
    af_note(
        "Campaign is profitable. Increase budget 20-50% on the best-performing channel."
    )
elif recommendation == "OPTIMIZE":
    af_note(
        "Campaign is breakeven-to-positive. Reallocate budget from low ROAS channels "
        "to top performer.",
        "warn",
    )
elif recommendation == "PAUSE":
    af_note(
        "Campaign is underwater but recoverable. Pause worst channel, rotate creative, retry.",
        "warn",
    )
else:
    af_note("Campaign is losing money. Kill it and redesign from scratch.", "bad")

if _agent_why:
    with st.expander("Agent rationale — recorded on the last graph run"):
        st.write(_agent_why)
        if not df_ts.empty:
            st.caption(
                "Recorded by roi_calculator at its last run; it may predate the KPI rows "
                "above. The verdict on this page is recomputed from the live data."
            )

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

fig = px.bar(
    df, x="Channel", y="ROAS", color="Verdict", title="ROAS by channel",
    color_discrete_map=_verdict_colors,
)
fig.update_layout(showlegend=False, **_LAYOUT)
fig.add_hline(y=4.0, line_dash="dot", line_color="#94A3B8")
st.plotly_chart(fig, use_container_width=True)
af_caption("Scale ≥ 4.0x · Optimize ≥ 2.0x · Pause ≥ 1.0x · Kill < 1.0x")

fig2 = px.bar(
    df, x="Channel", y=["Spend (USD)", "Revenue (USD)"],
    title="Spend vs revenue by channel", barmode="group",
    color_discrete_map={"Spend (USD)": "#94A3B8", "Revenue (USD)": ACCENT},
)
fig2.update_layout(**_LAYOUT)
st.plotly_chart(fig2, use_container_width=True)

st.dataframe(
    df,
    use_container_width=True,
    hide_index=True,
    column_config={
        "ROAS": st.column_config.NumberColumn("ROAS", format="%.2fx"),
        "Spend (USD)": st.column_config.NumberColumn("Spend", format="dollar"),
        "Revenue (USD)": st.column_config.NumberColumn("Revenue", format="dollar"),
        "CPA (USD)": st.column_config.NumberColumn("CPA", format="dollar"),
        "CTR (%)": st.column_config.NumberColumn("CTR", format="%.2f%%"),
    },
)

af_section("Time series", "Daily spend and ROAS per channel, last 30 days.")
if _ts_error:
    af_note(f"<b>Failed to load time series.</b> {html.escape(_ts_error)}", "warn")
elif not df_ts.empty:
    fig3 = px.line(
        df_ts, x="captured_at", y="spend_usd", color="channel",
        title="Daily spend by channel",
    )
    fig3.update_layout(**_LAYOUT)
    st.plotly_chart(fig3, use_container_width=True)
    fig4 = px.line(
        df_ts, x="captured_at", y="roas", color="channel",
        title="Daily ROAS by channel",
    )
    fig4.update_layout(**_LAYOUT)
    st.plotly_chart(fig4, use_container_width=True)
else:
    af_empty(
        "No time-series data",
        "No daily KPI snapshots were recorded for this campaign in the last 30 days.",
        hint="Campaigns → Run agent graph",
    )
