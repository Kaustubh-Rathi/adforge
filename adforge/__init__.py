"""AdForge — end-to-end multi-agent ad campaign platform.

Public API surface:
    adforge.api          — FastAPI app
    adforge.graph        — LangGraph orchestrator
    adforge.agents       — Per-agent feature modules
    adforge.mcp_servers  — Per-domain MCP servers
    adforge.guardrails   — Contracts, isolation, rate limits, audit log
    adforge.storage      — Postgres + Redis persistence
    adforge.llm          — LLM provider abstraction (GLM default)
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
