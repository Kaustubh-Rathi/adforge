"""Compliance MCP server — wraps the PolicyEnforcer for the Compliance agent.

Tools:
    evaluate_creative(creative_text, audience_targeting?)
    list_blocked_terms()
    list_required_disclosures(category)

The Compliance agent calls evaluate_creative to run the ruleset against the
creative text. This is the deterministic half of the compliance gate.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from adforge.guardrails.policy import PolicyEnforcer
from adforge.mcp_servers.permissions import readonly

mcp = FastMCP("adforge-compliance")
_enforcer = PolicyEnforcer.default()


@mcp.tool()
@readonly
async def evaluate_creative(
    creative_text: str,
    audience_targeting: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """Run creative text + audience targeting against the policy ruleset."""
    if not headers or "X-AdForge-Tenant" not in headers:
        return json.dumps({"error": "missing tenant header"})
    verdict = _enforcer.evaluate(creative_text, audience_targeting)
    return verdict.model_dump_json()


@mcp.tool()
@readonly
async def list_blocked_terms(headers: dict[str, str] | None = None) -> str:
    """Return the list of tier-1 (hard-blocked) patterns."""
    blocked = [
        {"rule_id": r.rule_id, "pattern": r.pattern, "description": r.description}
        for r in _enforcer.rules if r.tier == 1
    ]
    return json.dumps({"blocked": blocked})


@mcp.tool()
@readonly
async def list_required_disclosures(headers: dict[str, str] | None = None) -> str:
    """Return the list of tier-2 (require disclosure) rules."""
    disclosures = [
        {"rule_id": r.rule_id, "pattern": r.pattern,
         "required_disclosure": r.required_disclosure, "description": r.description}
        for r in _enforcer.rules if r.tier == 2
    ]
    return json.dumps({"disclosures": disclosures})
