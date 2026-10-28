"""ReAct-style Research Agent.

v0.1 was a single-shot LLM call — no actual tool-calling loop. v0.2 upgrades
to LangGraph's canonical ReAct pattern using ``create_agent`` + ``ToolNode``.

The agent now actually iterates:
    1. LLM reasons about what to research next.
    2. LLM picks a tool (web_search / serp_scrape / keyword_clusters /
       competitor_creatives / similarweb_traffic / appstore_search /
       ipinfo_lookup) and arguments.
    3. ToolNode executes the call via the MCP client.
    4. Result is added to messages.
    5. LLM iterates until it has enough to produce ResearchFindings.

The ``build_react_agent()`` factory is called once at module import time
and cached. It uses the Z.ai GLM provider (default) and the tools loaded
from the research + public_data MCP servers.

The original ``ResearchAgent`` (single-shot) is kept as a fallback for
tests where MCP servers aren't running.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from adforge.agents.research.persona import PERSONA
from adforge.guardrails.contracts import ResearchFindings
from adforge.llm.factory import get_llm
from adforge.mcp_servers.client import get_mcp_tools


@lru_cache
async def _load_research_tools() -> list:
    """Load tools from the research + public_data MCP servers.

    Returns an empty list if MCP servers are unreachable — caller should
    fall back to the single-shot ResearchAgent in that case.
    """
    research_tools = await get_mcp_tools("research")
    public_data_tools = await get_mcp_tools("public_data")
    return list(research_tools) + list(public_data_tools)


@lru_cache
def _build_react_agent():
    """Build the LangChain ReAct agent (create_agent) for research.

    Cached at module import. Tests can clear via ``_build_react_agent.cache_clear()``.
    """
    # Lazy imports so users who never use the ReAct path don't pay the install cost.
    from langchain.agents import create_agent
    from langgraph.prebuilt import ToolNode, tools_condition
    from langgraph.graph import StateGraph, START, END, MessagesState

    llm = get_llm()

    # We can't await inside _build_react_agent (it's sync), so we
    # structure this as a factory that returns a coroutine-returning builder.
    # The actual tool loading happens in ``run_react_research`` below.
    async def _build(tools: list) -> Any:
        model = llm  # ZaiGLMProvider implements chat() — bind_tools is via langchain wrapper

        # The langchain[mcp] adapter returns langchain_core.tools.Tool objects.
        # We need an LLM that can bind_tools — wrap our LLMProvider in a
        # minimal LangChain-compatible chat model.
        from adforge.llm.langchain_wrapper import LangChainLLMWrapper
        wrapped_model = LangChainLLMWrapper(llm)

        agent = create_agent(
            model=wrapped_model,
            tools=tools,
            system_prompt=PERSONA,
        )
        return agent

    return _build


async def run_react_research(query: str, max_iterations: int = 8) -> dict[str, Any]:
    """Run the ReAct research agent with a user query.

    Returns a ResearchFindings dict (validated against the schema).

    Falls back to single-shot if MCP tools aren't available.
    """
    tools = await _load_research_tools()
    if not tools:
        # Fallback: no MCP tools available, single-shot LLM call.
        from adforge.agents.research.agent import ResearchAgent
        from uuid import uuid4
        agent = ResearchAgent()
        # Synthesize a minimal AgentInput
        result = await agent.run({
            "request_id": str(uuid4()),
            "tenant_id": "react_tenant",
            "campaign_id": str(uuid4()),
            "agent_name": "research",
            "brief": None,
            "upstream_payloads": {},
        })
        return result.output.payload

    builder = _build_react_agent()
    agent = await builder(tools)

    # Invoke the ReAct agent
    initial_messages = [
        {"role": "system", "content": PERSONA},
        {"role": "user", "content": query + "\n\nProduce a ResearchFindings JSON object as your final answer."},
    ]
    final_state = await agent.ainvoke({"messages": initial_messages})

    # Extract the final assistant message — the LLM's ResearchFindings JSON
    last_msg = final_state["messages"][-1]
    content = last_msg.content if hasattr(last_msg, "content") else str(last_msg)

    # Strip markdown fences
    body = content.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1]
        if body.endswith("```"):
            body = body.rsplit("```", 1)[0]

    try:
        data = json.loads(body)
        return ResearchFindings(**data).model_dump()
    except Exception:
        return {
            "market_summary": body[:500] if body else "ReAct agent produced no parseable output.",
            "top_keywords": [], "sources": [],
            "_parse_error": "ReAct output not valid JSON",
        }
