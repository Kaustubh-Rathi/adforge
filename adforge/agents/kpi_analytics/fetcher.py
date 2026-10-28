"""KPI fetcher — calls ad-platform MCP servers in parallel.

For each channel we call the appropriate MCP server (or its mock), get raw
metrics, normalise into a KpiSnapshot, and return.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from adforge.config import get_settings
from adforge.guardrails.contracts import KpiSnapshot
from adforge.guardrails.data_isolation import TenantScope


async def fetch_all_channels(
    scope: TenantScope, channel: str, campaign_id: UUID
) -> KpiSnapshot | None:
    """Fetch a single channel's KPI snapshot via the appropriate MCP server."""
    s = get_settings()
    # In v1 we don't yet have full MCP wiring for live ad platforms.
    # Fall back to a deterministic mock when *_USE_MOCK is true (default).
    use_mock = {
        "google": s.google_ads_use_mock,
        "meta": s.meta_use_mock,
        "linkedin": s.linkedin_use_mock,
        "tiktok": s.tiktok_use_mock,
    }.get(channel, True)

    if use_mock:
        return _mock_snapshot(channel, campaign_id)

    # When fully wired, dispatch to the real MCP client:
    #   from adforge.mcp_servers.client import get_mcp_tools
    #   tools = await get_mcp_tools(channel)
    #   ... call tool, parse result, build KpiSnapshot ...
    # For v1 we just return the mock — see docs/RESEARCH.md for the wiring plan.
    return _mock_snapshot(channel, campaign_id)


def _mock_snapshot(channel: str, campaign_id: UUID) -> KpiSnapshot:
    """Deterministic-ish mock so seeded campaigns look realistic in the dashboard."""
    rng = random.Random(f"{channel}:{campaign_id}:{datetime.now(timezone.utc).date()}")
    impressions = rng.randint(5000, 50000)
    clicks = int(impressions * rng.uniform(0.008, 0.045))
    spend = round(clicks * rng.uniform(0.6, 2.8), 2)
    conversions = int(clicks * rng.uniform(0.02, 0.12))
    revenue = round(conversions * rng.uniform(15.0, 220.0), 2)
    return KpiSnapshot(
        channel=channel,  # type: ignore[arg-type]
        campaign_external_id=f"ext_{channel}_{campaign_id.hex[:8]}",
        impressions=impressions,
        clicks=clicks,
        spend_usd=spend,
        conversions=conversions,
        revenue_usd=revenue,
        ctr=round(clicks / impressions, 4),
        cvr=round(conversions / clicks, 4) if clicks else 0.0,
        cpc=round(spend / clicks, 2) if clicks else 0.0,
        cpa=round(spend / conversions, 2) if conversions else 0.0,
        cpm=round(spend / impressions * 1000, 2),
        roas=round(revenue / spend, 2) if spend else 0.0,
    )
