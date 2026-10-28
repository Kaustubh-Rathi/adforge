"""Ads MCP server — composite that exposes Google/Meta/LinkedIn/TikTok ad tools.

This is the agent-facing surface. Under the hood:
    - Google Ads read path   → delegates to official googleads/google-ads-mcp
    - Google Ads write path  → wraps google-ads-python directly (no MCP)
    - Meta + TikTok          → delegates to Pipeboard remote MCP (OAuth)
    - LinkedIn               → uses adforge.mcp_servers.ads.linkedin (our wrapper)

PERMISSIONS (v0.2+):
    - All READ tools use @readonly — no approval needed.
    - All WRITE tools (publish_campaign, pause_campaign, update_budget) use
      @write_operation(requires_approval=True). They require a valid
      X-AdForge-Approval-Token issued by the HITL review endpoint.
    - The LLM is NEVER given write tool schemas in its prompt — only the
      orchestrator's post-approval publish step can call them.

For v1 we expose a thin set of tools. The actual MCP-to-MCP delegation is
handled by ``adforge.mcp_servers.client.get_mcp_tools`` on the agent side.
"""
from __future__ import annotations

import json
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP

from adforge.mcp_servers.permissions import readonly, write_operation

mcp = FastMCP("adforge-ads")


@mcp.tool()
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


@mcp.tool()
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

    WRITE OPERATION — requires HITL approval. The orchestrator's publish
    step calls this AFTER the human reviewer has approved the creative +
    media plan. Without a valid X-AdForge-Approval-Token, this returns
    ``{"error": "approval_required"}``.
    """
    if budget_usd <= 0:
        return json.dumps({"error": "budget must be positive"})

    # v1: return a mock external ID. Production: call the platform SDK.
    external_id = f"{channel}_cpg_{secrets.token_hex(8)}"
    return json.dumps({
        "channel": channel,
        "external_campaign_id": external_id,
        "status": "pending_review" if channel == "linkedin" else "active",
        "budget_usd": budget_usd,
        "published_with_approval": True,
    })


@mcp.tool()
@write_operation(requires_approval=True)
async def pause_campaign(
    channel: str, external_campaign_id: str, headers: dict[str, str] | None = None
) -> str:
    """Pause a live campaign. Used by the Optimization agent — but only after
    a human reviewer approves the pause action."""
    return json.dumps({
        "channel": channel,
        "external_campaign_id": external_campaign_id,
        "status": "paused",
        "paused_with_approval": True,
    })


@mcp.tool()
@write_operation(requires_approval=True)
async def update_budget(
    channel: str,
    external_campaign_id: str,
    new_budget_usd: float,
    headers: dict[str, str] | None = None,
) -> str:
    """Update the daily budget of a live campaign. WRITE OPERATION — HITL gated."""
    if new_budget_usd <= 0:
        return json.dumps({"error": "budget must be positive"})
    return json.dumps({
        "channel": channel,
        "external_campaign_id": external_campaign_id,
        "daily_budget_usd": new_budget_usd,
        "status": "updated",
        "updated_with_approval": True,
    })
