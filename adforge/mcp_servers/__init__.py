"""AdForge MCP servers package.

We ship FIVE local MCP servers (one per domain) plus integrate TWO external
MCP servers:

LOCAL (we write these):
    adforge/mcp_servers/research.py     — Tavily web search + SERP scrape
    adforge/mcp_servers/analytics.py    — BigQuery/Postgres KPI persistence
    adforge/mcp_servers/roi.py          — ROI formula computation (pure)
    adforge/mcp_servers/compliance.py   — policy rules lookup
    adforge/mcp_servers/ads/linkedin.py — LinkedIn Marketing wrapper (no official MCP exists)

EXTERNAL (we integrate, see docs/RESEARCH.md):
    googleads/google-ads-mcp           — official Google Ads read-only MCP
    pipeboard-co/meta-ads-mcp family   — Meta + TikTok + Snap + Reddit via OAuth

All local servers use the official Anthropic MCP Python SDK (``mcp[cli]``)
via ``FastMCP``.
"""
from adforge.mcp_servers.runner import run_server

__all__ = ["run_server"]
