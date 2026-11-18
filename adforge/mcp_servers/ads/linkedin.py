"""LinkedIn Ads MCP server — read-only reporting through LinkedIn's official
Marketing API (``/rest/adAnalytics``).

This intentionally does not use the unofficial ``linkedin-api`` package: that
library targets LinkedIn's private website API, is not an Ads SDK, and pins an
old ``lxml`` range that has no Python 3.14 wheels.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

import httpx
from mcp.server.fastmcp import FastMCP

from adforge.agents.kpi_analytics.fetcher import fetch_linkedin
from adforge.config import get_settings
from adforge.mcp_servers.permissions import READ_ONLY, readonly

mcp = FastMCP("adforge-linkedin-ads")

_NAMESPACE = UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")


def _check_auth(headers: dict[str, str] | None) -> str:
    if not headers or "X-AdForge-Tenant" not in headers:
        raise ValueError("Missing X-AdForge-Tenant header — refusing to serve")
    return headers["X-AdForge-Tenant"]


@mcp.tool(annotations=READ_ONLY)
@readonly
async def get_ad_analytics(
    ad_account_id: str,
    campaign_ids: list[str] | None = None,
    days_back: int = 7,
    headers: dict[str, str] | None = None,
) -> str:
    """Fetch daily LinkedIn ad analytics for an ad account.

    Returns real impressions, clicks, spend, conversions, and revenue. The
    account id and campaign ids are the LinkedIn numeric ids (without URN
    prefixes).
    """
    tenant_id = _check_auth(headers)
    s = get_settings()
    if not s.linkedin_access_token:
        return json.dumps({
            "error": "linkedin_not_configured",
            "detail": "Set LINKEDIN_ACCESS_TOKEN to enable LinkedIn reporting.",
        })

    campaign_ids = [c for c in (campaign_ids or []) if str(c).strip()]
    header_campaign = (headers or {}).get("X-AdForge-Campaign", "")
    if not campaign_ids and header_campaign:
        campaign_ids = [header_campaign]
    if not campaign_ids:
        return json.dumps({
            "error": "campaign_id_required",
            "detail": "Pass campaign_ids or the X-AdForge-Campaign header.",
        })
    if days_back < 1 or days_back > 90:
        return json.dumps({"error": "days_back must be between 1 and 90"})

    end = datetime.now(UTC).date()
    start = end - timedelta(days=days_back - 1)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    async with httpx.AsyncClient(
        timeout=s.external_http_timeout, trust_env=False
    ) as client:
        for external_id in campaign_ids:
            internal_id = uuid5(_NAMESPACE, f"{tenant_id}:{external_id}")
            try:
                snapshots = await fetch_linkedin(
                    client,
                    campaign_id=internal_id,
                    external_id=str(external_id),
                    start=start,
                    end=end,
                    ad_account_id=ad_account_id,
                )
                rows.extend(s.model_dump(mode="json") for s in snapshots)
            except Exception as exc:  # noqa: BLE001 - report per campaign
                errors.append(f"{external_id}: {type(exc).__name__}: {exc}")

    return json.dumps({
        "ad_account_id": ad_account_id,
        "campaign_ids": campaign_ids,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "daily": rows,
        "count": len(rows),
        "errors": errors,
    })
