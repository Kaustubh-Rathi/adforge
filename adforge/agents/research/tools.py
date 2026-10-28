"""Tools available to the Research agent.

These are thin wrappers that call the AdForge research MCP server
(see ``adforge/mcp_servers/research``). The MCP server itself backs onto
Tavily for web search + a small SERP scraper.
"""
from __future__ import annotations

from typing import Any

from adforge.guardrails.data_isolation import TenantScope
from adforge.guardrails.rate_limiter import get_rate_limiter
from adforge.mcp_servers.client import get_mcp_tools


async def call_research_tool(
    scope: TenantScope, tool_name: str, **kwargs: Any
) -> dict[str, Any]:
    """Call a tool on the AdForge research MCP server.

    The MCP server enforces tenant_id from the X-AdForge-Tenant header —
    we can never accidentally pull another tenant's cached research.
    """
    await get_rate_limiter("llm").acquire(
        "llm", tenant_id=scope.tenant_id, campaign_id=str(scope.campaign_id)
    )
    tools = await get_mcp_tools("research")
    headers = scope.asheaders()

    # The langchain[mcp] adapter exposes tools as LangChain Tool objects;
    # each has an async ainvoke method that accepts a kwargs dict.
    for t in tools:
        if t.name == tool_name:
            # Inject tenant headers as the first kwarg; MCP server reads them.
            result = await t.ainvoke({**kwargs, "headers": headers})
            return {"tool": tool_name, "result": result}

    raise KeyError(f"research MCP server has no tool named {tool_name!r}")
