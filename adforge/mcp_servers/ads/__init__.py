"""Ads MCP server — composite that exposes Google/Meta/LinkedIn/TikTok ad tools.

READ tools (``@readonly``) are safe for agents. WRITE tools
(``publish_campaign``, ``pause_campaign``, ``update_budget``) are
``@write_operation(requires_approval=True)`` and are never exposed to the LLM.

Publishing is delegated to a configured adapter (see ``writes.py``). There is
no simulated success path: if writes are disabled or no adapter is configured,
the tool returns an explicit error and nothing is launched.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from adforge.mcp_servers.ads import writes
from adforge.mcp_servers.permissions import READ_ONLY, WRITE, readonly, write_operation

mcp = FastMCP("adforge-ads")


@mcp.tool(annotations=READ_ONLY)
@readonly
async def list_channels(headers: dict[str, str] | None = None) -> str:
    """Return the list of ad platforms AdForge supports."""
    return json.dumps({
        "channels": [
            {"id": "google", "name": "Google Ads", "capabilities": ["search", "display", "pmax", "youtube"]},
            {"id": "meta", "name": "Meta Ads (Facebook + Instagram)", "capabilities": ["feed", "stories", "reels"]},
            {"id": "linkedin", "name": "LinkedIn Marketing Solutions", "capabilities": ["sponsored_content", "message_ads", "inmail"]},
            {"id": "tiktok", "name": "TikTok Ads", "capabilities": ["in_feed", "top_view", "spark_ads"]},
        ]
    })


@mcp.tool(annotations=WRITE)
@write_operation(requires_approval=True)
async def publish_campaign(
    channel: str,
    campaign_name: str,
    budget_usd: float,
    creative: dict[str, Any],
    audience: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> str:
    """Publish a campaign to the given ad platform. Returns the external campaign ID.

    WRITE OPERATION — requires HITL approval. The orchestrator calls this only
    after a human reviewer approved the creative + media plan and the platform
    adapter is explicitly configured.
    """
    try:
        result = await writes.publish_campaign(
            channel=channel,
            campaign_name=campaign_name,
            budget_usd=budget_usd,
            creative=creative,
            audience=audience,
        )
    except writes.WriteGatewayError as exc:
        return json.dumps({"error": "publish_failed", "detail": str(exc)})
    return json.dumps(result)


@mcp.tool(annotations=WRITE)
@write_operation(requires_approval=True)
async def pause_campaign(
    channel: str, external_campaign_id: str, headers: dict[str, str] | None = None
) -> str:
    """Pause a live campaign after a human reviewer approves the pause."""
    try:
        result = await writes.pause_campaign(
            channel=channel, external_campaign_id=external_campaign_id
        )
    except writes.WriteGatewayError as exc:
        return json.dumps({"error": "pause_failed", "detail": str(exc)})
    return json.dumps(result)


@mcp.tool(annotations=WRITE)
@write_operation(requires_approval=True)
async def update_budget(
    channel: str,
    external_campaign_id: str,
    new_budget_usd: float,
    headers: dict[str, str] | None = None,
) -> str:
    """Update the daily budget of a live campaign. WRITE OPERATION — HITL gated."""
    try:
        result = await writes.update_budget(
            channel=channel,
            external_campaign_id=external_campaign_id,
            new_budget_usd=new_budget_usd,
        )
    except writes.WriteGatewayError as exc:
        return json.dumps({"error": "budget_update_failed", "detail": str(exc)})
    return json.dumps(result)
