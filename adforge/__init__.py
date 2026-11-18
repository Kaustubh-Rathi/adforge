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

import sys as _sys

# psycopg's async pool (used by LangGraph's Postgres checkpointer) cannot run
# on Windows' default ProactorEventLoop. Select the selector loop at package
# import time, before any entrypoint calls asyncio.run().
if _sys.platform == "win32":  # pragma: no cover - platform-specific
    import asyncio as _asyncio

    try:
        _asyncio.set_event_loop_policy(_asyncio.WindowsSelectorEventLoopPolicy())
    except AttributeError:  # pragma: no cover - future Python may remove it
        pass

__version__ = "0.1.0"
__all__ = ["__version__"]
