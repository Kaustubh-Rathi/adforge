"""MCP client — connects agents to all MCP servers via a single
``MultiServerMCPClient`` from ``langchain[mcp]``.

This is the integration point with the open-source ecosystem:
    - AdForge's 7 local MCP servers (research, ads, analytics, roi,
      compliance, public data, LinkedIn)
    - googleads/google-ads-mcp (official, read-only Google Ads)
    - Pipeboard remote MCPs (Meta + TikTok + Snap + Reddit, OAuth)

Agents call ``get_mcp_tools("research")`` and get a list of LangChain Tools
backed by the appropriate MCP server. Tools are cached per server name.
"""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

from adforge.config import get_settings

if TYPE_CHECKING:
    from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger("adforge.mcp")

ServerName = Literal[
    "research",
    "ads",
    "analytics",
    "roi",
    "compliance",
    "public_data",
    "linkedin",
    "google_ads_official",
    "meta",
    "tiktok",
    "snap",
    "reddit",
]


@lru_cache
def _build_client() -> MultiServerMCPClient:
    """Construct a MultiServerMCPClient with all configured servers.

    Servers without credentials (e.g. Pipeboard when no token is set) are
    skipped. Tools from a skipped server are simply unavailable; callers get an
    explicit configuration error from the remaining tools.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient  # post-migration name

    s = get_settings()
    servers: dict[str, Any] = {}

    # Local servers (always available)
    if s.mcp_transport == "sse":
        servers["research"] = {"transport": "sse", "url": s.mcp_url("research")}
        servers["ads"] = {"transport": "sse", "url": s.mcp_url("ads")}
        servers["analytics"] = {"transport": "sse", "url": s.mcp_url("analytics")}
        servers["roi"] = {"transport": "sse", "url": s.mcp_url("roi")}
        servers["compliance"] = {"transport": "sse", "url": s.mcp_url("compliance")}
        servers["public_data"] = {"transport": "sse", "url": s.mcp_url("public_data")}
        servers["linkedin"] = {"transport": "sse", "url": s.mcp_url("linkedin")}
    else:
        # stdio transport for local dev / tests. Use the current interpreter
        # so the MCP subprocess sees the same virtualenv + installed package.
        for name in (
            "research",
            "ads",
            "analytics",
            "roi",
            "compliance",
            "public_data",
            "linkedin",
        ):
            servers[name] = {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "adforge.mcp_servers.runner", name],
            }

    # External servers — only include when configured
    if s.pipeboard_api_token:
        for name, url in (
            ("meta", s.pipeboard_meta_url),
            ("tiktok", s.pipeboard_tiktok_url),
            ("snap", s.pipeboard_snap_url),
            ("reddit", s.pipeboard_reddit_url),
        ):
            servers[name] = {
                "transport": "sse",
                "url": url,
                "headers": {"Authorization": f"Bearer {s.pipeboard_api_token}"},
            }

    if s.google_application_credentials:
        # The official googleads/google-ads-mcp runs over streamable_http in
        # its own container (see docker-compose.yml).
        servers["google_ads_official"] = {
            "transport": "streamable_http",
            "url": s.mcp_google_ads_official_url
            or f"{s.mcp_scheme}://{s.mcp_host}:{s.mcp_google_ads_official_port}/mcp",
        }

    return MultiServerMCPClient(servers)


def _is_read_only(tool: object) -> bool:
    """Fail closed: only tools explicitly annotated read-only are exposed."""
    metadata = getattr(tool, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return False
    return metadata.get("readOnlyHint") is True and metadata.get("destructiveHint") is not True


async def get_mcp_tools(server: ServerName, *, read_only: bool = True) -> list[Any]:
    """Return LangChain Tools exposed by the given MCP server.

    ``read_only`` defaults to ``True`` so an LLM-facing caller can never
    receive a write tool. Only tools carrying the MCP ``readOnlyHint``
    annotation are returned; unannotated tools fail closed. The orchestrator's
    approval-gated publish path passes ``read_only=False`` explicitly.

    Returns an empty list if the server is unreachable or not configured.
    """
    s = get_settings()
    if not s.mcp_enabled:
        return []

    try:
        client = _build_client()
        tools = await client.get_tools(server_name=server)
    except Exception:
        # Best-effort — agents degrade gracefully. Log so a misconfigured MCP
        # server is visible instead of silently returning no tools.
        logger.warning("MCP server %r unavailable", server, exc_info=True)
        return []

    if read_only:
        return [tool for tool in tools if _is_read_only(tool)]
    return list(tools)


def reset_mcp_client() -> None:
    """Clear the cached client. Used by tests."""
    _build_client.cache_clear()
