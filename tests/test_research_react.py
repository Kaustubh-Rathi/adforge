"""Tests for the ReAct research agent and its tool-dispatch helper."""
from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from support import VALID_RESEARCH, ScriptedLLM

import adforge.agents.base as agent_base
import adforge.agents.research.react_agent as react_agent
import adforge.agents.research.tools as research_tools
import adforge.llm.langchain_wrapper as langchain_wrapper
from adforge.agents.research.persona import PERSONA
from adforge.agents.research.react_agent import (
    _build_react_agent,
    _load_research_tools,
    run_react_research,
)
from adforge.agents.research.tools import call_research_tool
from adforge.guardrails.data_isolation import TenantScope


class _FakeLimiter:
    def __init__(self) -> None:
        self.acquisitions: list[tuple[str, str, str]] = []

    async def acquire(self, scope, *, tenant_id: str, campaign_id: str) -> None:
        self.acquisitions.append((scope, tenant_id, campaign_id))


class _FakeTool:
    name = "web_search"

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def ainvoke(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return {"results": [{"title": "B2B CRM market report"}]}


async def test_call_research_tool_injects_tenant_headers(monkeypatch):
    scope = TenantScope(tenant_id="acme", campaign_id=uuid4())
    limiter = _FakeLimiter()
    tool = _FakeTool()

    async def fake_get_mcp_tools(server):
        assert server == "research"
        return [tool]

    monkeypatch.setattr(research_tools, "get_rate_limiter", lambda _scope: limiter)
    monkeypatch.setattr(research_tools, "get_mcp_tools", fake_get_mcp_tools)

    result = await call_research_tool(scope, "web_search", query="best b2b crm")

    assert result == {
        "tool": "web_search",
        "result": {"results": [{"title": "B2B CRM market report"}]},
    }
    assert limiter.acquisitions == [("llm", "acme", str(scope.campaign_id))]
    assert len(tool.payloads) == 1
    payload = tool.payloads[0]
    assert payload["query"] == "best b2b crm"
    assert payload["headers"] == {
        "X-AdForge-Tenant": "acme",
        "X-AdForge-Campaign": str(scope.campaign_id),
    }


async def test_call_research_tool_unknown_name_raises_key_error(monkeypatch):
    scope = TenantScope(tenant_id="acme", campaign_id=uuid4())
    limiter = _FakeLimiter()

    async def fake_get_mcp_tools(_server):
        return [_FakeTool()]

    monkeypatch.setattr(research_tools, "get_rate_limiter", lambda _scope: limiter)
    monkeypatch.setattr(research_tools, "get_mcp_tools", fake_get_mcp_tools)

    with pytest.raises(KeyError, match="no tool named"):
        await call_research_tool(scope, "does_not_exist")


async def test_run_react_research_falls_back_without_tools(monkeypatch):
    async def no_tools():
        return []

    scripted = ScriptedLLM(
        responses={"Produce a ResearchFindings JSON object": VALID_RESEARCH}
    )
    monkeypatch.setattr(react_agent, "_load_research_tools", no_tools)
    monkeypatch.setattr(agent_base, "get_llm", lambda: scripted)

    result = await run_react_research(
        "Research the B2B CRM market",
        tenant_id="acme",
        campaign_id=str(uuid4()),
    )

    assert result == VALID_RESEARCH
    assert len(scripted.calls) == 1


class _FakeAgent:
    def __init__(self, content: str) -> None:
        self.content = content

    async def ainvoke(self, _state, config=None):
        return {"messages": [SimpleNamespace(content=self.content)]}


def _patch_react_tools(monkeypatch, content: str) -> None:
    async def fake_load_tools():
        return [object()]

    async def fake_builder(_tools):
        return _FakeAgent(content)

    monkeypatch.setattr(react_agent, "_load_research_tools", fake_load_tools)
    monkeypatch.setattr(react_agent, "_build_react_agent", lambda: fake_builder)


async def test_run_react_research_parses_fenced_json(monkeypatch):
    fenced = "```json\n" + json.dumps(VALID_RESEARCH) + "\n```"
    _patch_react_tools(monkeypatch, fenced)

    result = await run_react_research(
        "Research the B2B CRM market",
        tenant_id="acme",
        campaign_id=str(uuid4()),
    )

    assert result == VALID_RESEARCH


async def test_run_react_research_reports_unparseable_output(monkeypatch):
    body = "The tools returned no usable data. " + ("x" * 600)
    _patch_react_tools(monkeypatch, body)

    result = await run_react_research(
        "Research the B2B CRM market",
        tenant_id="acme",
        campaign_id=str(uuid4()),
    )

    assert result["_parse_error"] == "ReAct output not valid JSON"
    assert result["top_keywords"] == []
    assert result["sources"] == []
    assert result["market_summary"] == body[:500]
    assert len(result["market_summary"]) == 500


async def test_load_research_tools_concatenates_both_servers(monkeypatch):
    calls: list[str] = []

    async def fake_get_mcp_tools(server):
        calls.append(server)
        return {
            "research": ["web_search", "serp_scrape"],
            "public_data": ["similarweb_traffic"],
        }[server]

    monkeypatch.setattr(react_agent, "get_mcp_tools", fake_get_mcp_tools)
    _load_research_tools.cache_clear()
    try:
        tools = await _load_research_tools()
        # A cached async result must be safely awaitable more than once.
        again = await _load_research_tools()
    finally:
        _load_research_tools.cache_clear()

    assert tools == ["web_search", "serp_scrape", "similarweb_traffic"]
    assert again == tools
    assert calls == ["research", "public_data"]


async def test_build_react_agent_builds_wrapper_and_agent(monkeypatch):
    fake_llm = ScriptedLLM(default={})
    captured: dict = {}

    class FakeWrapper:
        def __init__(self, provider):
            self.provider = provider

    def fake_create_agent(*, model, tools, system_prompt):
        captured.update(model=model, tools=tools, system_prompt=system_prompt)
        return "agent-sentinel"

    import langchain.agents

    monkeypatch.setattr(react_agent, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(langchain_wrapper, "LangChainLLMWrapper", FakeWrapper)
    monkeypatch.setattr(langchain.agents, "create_agent", fake_create_agent)

    _build_react_agent.cache_clear()
    try:
        builder = _build_react_agent()
        assert callable(builder)
        fake_tool = object()
        agent = await builder([fake_tool])
    finally:
        _build_react_agent.cache_clear()

    assert agent == "agent-sentinel"
    assert isinstance(captured["model"], FakeWrapper)
    assert captured["model"].provider is fake_llm
    assert captured["tools"] == [fake_tool]
    assert captured["system_prompt"] == PERSONA
