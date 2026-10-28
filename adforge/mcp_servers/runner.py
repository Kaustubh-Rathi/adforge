"""Single entrypoint that boots any AdForge MCP server by name.

Usage:
    python -m adforge.mcp_servers.runner research
    python -m adforge.mcp_servers.runner ads
    python -m adforge.mcp_servers.runner analytics
    python -m adforge.mcp_servers.runner roi
    python -m adforge.mcp_servers.runner compliance
    python -m adforge.mcp_servers.runner linkedin

Each server runs in its own process — see docker-compose.yml. This lets us
distribute load across instances instead of cramming every tool onto one process.
"""
from __future__ import annotations

import sys


SERVERS = {
    "research": "adforge.mcp_servers.research:mcp",
    "ads": "adforge.mcp_servers.ads:mcp",  # composite — wraps LinkedIn + delegates to google-ads-mcp + pipeboard
    "analytics": "adforge.mcp_servers.analytics:mcp",
    "roi": "adforge.mcp_servers.roi:mcp",
    "compliance": "adforge.mcp_servers.compliance:mcp",
    "linkedin": "adforge.mcp_servers.ads.linkedin:mcp",
    "public_data": "adforge.mcp_servers.public_data:mcp",
}


def run_server(name: str) -> None:
    if name not in SERVERS:
        print(f"Unknown MCP server: {name!r}", file=sys.stderr)
        print(f"Available: {', '.join(SERVERS)}", file=sys.stderr)
        sys.exit(2)

    from adforge.config import get_settings
    s = get_settings()

    module_path, attr = SERVERS[name].split(":")
    import importlib
    mod = importlib.import_module(module_path)
    mcp = getattr(mod, attr)

    # FastMCP picks up transport from env / args; default to sse so the API
    # container can talk to us remotely.
    transport = s.mcp_transport
    if transport == "sse":
        port_map = {
            "research": s.mcp_research_port,
            "ads": s.mcp_ads_port,
            "analytics": s.mcp_analytics_port,
            "roi": s.mcp_roi_port,
            "compliance": s.mcp_compliance_port,
            "linkedin": s.mcp_ads_port + 10,
            "public_data": s.mcp_public_data_port,
        }
        port = port_map.get(name, 9000)
        mcp.run(transport="sse", port=port)
    else:
        mcp.run(transport=transport)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m adforge.mcp_servers.runner <name>", file=sys.stderr)
        sys.exit(2)
    run_server(sys.argv[1])
