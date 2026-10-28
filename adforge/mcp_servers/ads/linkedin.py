"""LinkedIn Ads MCP server — AdForge's custom wrapper.

No official LinkedIn MCP exists (as of Oct 2026). We wrap the
community-maintained `linkedin-api` Python library and expose its
ad-account + reporting endpoints as MCP tools.

For v1 we expose a minimal tool set — enough for the KPI agent to pull
impressions + spend. Production version would add create_campaign, manage_audiences, etc.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from adforge.config import get_settings
from adforge.mcp_servers.permissions import readonly

mcp = FastMCP("adforge-linkedin-ads")


def _check_auth(headers: dict[str, str] | None) -> None:
    if not headers or "X-AdForge-Tenant" not in headers:
        raise ValueError("Missing X-AdForge-Tenant header — refusing to serve")


@mcp.tool()
@readonly
async def get_ad_analytics(
    ad_account_id: str,
    campaign_ids: list[str] | None = None,
    days_back: int = 7,
    headers: dict[str, str] | None = None,
) -> str:
    """Fetch ad analytics for a LinkedIn ad account.

    Returns JSON with impressions, clicks, spend, conversions per campaign.
    """
    _check_auth(headers)
    s = get_settings()
    if s.linkedin_use_mock or not s.linkedin_access_token:
        # Mock — return plausible-looking numbers
        from datetime import datetime, timedelta, timezone
        days = []
        for i in range(days_back):
            day = (datetime.now(timezone.utc) - timedelta(days=i)).date().isoformat()
            days.append({
                "date": day,
                "impressions": 4500 + i * 120,
                "clicks": 110 + i * 4,
                "spend_usd": round(180.0 + i * 8.5, 2),
                "conversions": 5 + i // 2,
            })
        return json.dumps({"ad_account_id": ad_account_id, "daily": days, "mock": True})

    # Production: use linkedin-api client to call /v2/adAnalytics
    # For v1 we don't wire it — keep mock. See docs/RESEARCH.md.
    return json.dumps({"error": "LinkedIn live API not wired in v1 — use mock mode"})
