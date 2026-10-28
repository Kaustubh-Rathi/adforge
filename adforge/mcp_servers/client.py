"""MCP client — connects agents to all MCP servers via a single
``MultiServerMCPClient`` from ``langchain[mcp]``.

This is the integration point with the open-source ecosystem:
    - AdForge's 5 local MCP servers (research, ads, analytics, roi, compliance)
    - googleads/google-ads-mcp (official, read-only Google Ads)
    - Pipeboard remote MCPs (Meta + TikTok + Snap + Reddit, OAuth)

Agents call ``get_mcp_tools("research")`` and get a list of LangChain Tools
backed by the appropriate MCP server. Tools are cached per server name.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from adforge.config import get_settings

ServerName = Literal["research", "ads", "analytics", "roi", "compliance", "google_ads_official"]


@lru_cache
def _build_client() -> "MultiServerMCPClient":  # type: ignore[name-defined]
    """Construct a MultiServerMCPClient with all configured servers.

    Servers without credentials (e.g. Pipeboard when no token is set) are
    skipped — the agent will fall back to mock mode.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient  # post-migration name

    s = get_settings()
    servers: dict[str, dict] = {}

    # Local servers (always available)
    if s.mcp_transport == "sse":
        servers["research"] = {"transport": "sse", "url": s.mcp_url("research")}
        servers["ads"] = {"transport": "sse", "url": s.mcp_url("ads")}
        servers["analytics"] = {"transport": "sse", "url": s.mcp_url("analytics")}
        servers["roi"] = {"transport": "sse", "url": s.mcp_url("roi")}
        servers["compliance"] = {"transport": "sse", "url": s.mcp_url("compliance")}
        servers["public_data"] = {"transport": "sse", "url": s.mcp_url("public_data")}
    else:
        # stdio transport for local dev / tests
        servers["research"] = {
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "adforge.mcp_servers.runner", "research"],
        }
        servers["ads"] = {
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "adforge.mcp_servers.runner", "ads"],
        }
        servers["analytics"] = {
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "adforge.mcp_servers.runner", "analytics"],
        }
        servers["roi"] = {
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "adforge.mcp_servers.runner", "roi"],
        }
        servers["compliance"] = {
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "adforge.mcp_servers.runner", "compliance"],
        }
        servers["public_data"] = {
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "adforge.mcp_servers.runner", "public_data"],
        }

    # External servers — only include when configured
    if s.pipeboard_api_token:
        servers["meta"] = {
            "transport": "sse",
            "url": s.pipeboard_meta_url,
            "headers": {"Authorization": f"Bearer {s.pipeboard_api_token}"},
        }
        servers["tiktok"] = {
            "transport": "sse",
            "url": s.pipeboard_tiktok_url,
            "headers": {"Authorization": f"Bearer {s.pipeboard_api_token}"},
        }

    if s.google_application_credentials:
        # The official googleads/google-ads-mcp runs over streamable_http in
        # its own container (see docker-compose.yml).
        servers["google_ads_official"] = {
            "transport": "streamable_http",
            "url": f"http://localhost:{s.mcp_google_ads_official_port}/mcp",
        }

    return MultiServerMCPClient(servers)


@lru_cache
def _build_client_noop() -> list:  # pragma: no cover
    """Used in test/dev when MCP servers aren't running — returns no tools."""
    return []


async def get_mcp_tools(server: ServerName) -> list:
    """Return the list of LangChain Tools exposed by the given MCP server.

    Returns an empty list if the server is unreachable or not configured.
    The agent's tool-call code must handle the empty case gracefully.
    """
    s = get_settings()
    # In test mode without guardrails, skip MCP entirely.
    if not s.guardrails_active and s.app_env == "test":
        return []

    try:
        client = _build_client()
        return await client.get_tools(server)
    except Exception:
        # Best-effort — agents should degrade gracefully to mock mode.
        return []


def reset_mcp_client() -> None:
    """Clear the cached client. Used by tests."""
    _build_client.cache_clear()
