"""ROI MCP server — pure-formula financial calculations.

Tools:
    compute_roas(revenue, spend)
    compute_cpa(spend, conversions)
    compute_payback_period(cac, ltv, gross_margin_pct)
    classify_recommendation(roas, payback_days) -> scale|optimize|pause|kill

This server is intentionally pure-Python — no DB calls, no LLM calls. The
ROI agent calls these as deterministic functions to sanity-check the LLM's
verdict before persisting.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from adforge.mcp_servers.permissions import readonly

mcp = FastMCP("adforge-roi")


@mcp.tool()
@readonly
async def compute_roas(revenue_usd: float, spend_usd: float) -> str:
    if spend_usd <= 0:
        return json.dumps({"error": "spend must be positive"})
    return json.dumps({"roas": round(revenue_usd / spend_usd, 4)})


@mcp.tool()
@readonly
async def compute_cpa(spend_usd: float, conversions: int) -> str:
    if conversions <= 0:
        return json.dumps({"error": "conversions must be positive"})
    return json.dumps({"cpa": round(spend_usd / conversions, 2)})


@mcp.tool()
@readonly
async def compute_payback_period(
    cac_usd: float, ltv_usd: float, gross_margin_pct: float
) -> str:
    """Days to recoup customer acquisition cost. LTV * gross_margin / 365 = daily profit.
    payback_days = cac / (ltv * margin / 365)"""
    if ltv_usd <= 0 or gross_margin_pct <= 0:
        return json.dumps({"error": "ltv and gross_margin must be positive"})
    daily_profit = (ltv_usd * (gross_margin_pct / 100.0)) / 365.0
    if daily_profit <= 0:
        return json.dumps({"payback_period_days": None, "error": "non-positive daily profit"})
    return json.dumps({"payback_period_days": round(cac_usd / daily_profit, 1)})


@mcp.tool()
@readonly
async def classify_recommendation(roas: float, payback_days: float | None = None) -> str:
    if roas >= 4.0 and (payback_days is None or payback_days < 30):
        rec = "scale"
    elif roas >= 2.0 and (payback_days is None or payback_days < 60):
        rec = "optimize"
    elif roas >= 1.0:
        rec = "pause"
    else:
        rec = "kill"
    return json.dumps({"recommendation": rec, "roas": roas, "payback_days": payback_days})
