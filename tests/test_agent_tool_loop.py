"""Tests for the LLM-facing read-only MCP tool loop in ``BaseAgent``.

The loop is the only place an LLM can reach an MCP tool. These tests pin the
safety properties:

* only tools explicitly annotated read-only are loaded,
* write/unannotated tools can never be invoked by name,
* tenant headers are injected by the orchestrator, never the model,
* tool results are fed back and truncated,
* an unbounded tool-calling model fails closed at the iteration limit.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from support import ScriptedLLM

import adforge.mcp_servers.client as mcp_client
from adforge.agents.base import BaseAgent
from adforge.guardrails.contracts import AgentInput
from adforge.llm.base import LLMResponse, LLMUsage, ToolCall

VALID_PAYLOAD = {"ok": True, "summary": "tool-assisted answer"}


class FakeTool:
    """Minimal LangChain-tool stand-in with a permissive schema."""

    def __init__(self, name: str, *, result: Any = None) -> None:
        self.name = name
        self.description = f"Fake {name} tool"
        self.args_schema = {
            "type": "object",
            "title": "Args",
            "properties": {
                "query": {"type": "string"},
                "headers": {"type": "object"},
            },
            "required": ["query", "headers"],
        }
        self.calls: list[dict[str, Any]] = []
        self._result = result if result is not None else {"tool": name, "ok": True}

    async def ainvoke(self, arguments: dict[str, Any]) -> Any:
        self.calls.append(arguments)
        return self._result


class ToolLoopAgent(BaseAgent):
    name = "tool_loop_test"
    persona = "You are a test agent."
    mcp_servers = ("research",)

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.persona},
            {"role": "user", "content": "Answer using the available tools."},
        ]

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        return json.loads(content)


def _input() -> dict[str, Any]:
    return {
        "request_id": str(uuid4()),
        "tenant_id": "tenant_a",
        "campaign_id": str(uuid4()),
        "agent_name": "tool_loop_test",
        "upstream_payloads": {},
    }


def _tool_response(name: str, arguments: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        content="",
        usage=LLMUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12),
        tool_calls=[ToolCall(id="call_1", name=name, arguments=arguments)],
        finish_reason="tool_calls",
    )


def _final_response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(VALID_PAYLOAD),
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        finish_reason="stop",
    )


@pytest.fixture
def patched_tools(monkeypatch):
    """Install a fake MCP client and record how it was asked for tools."""

    def _install(tools: list[FakeTool]):
        calls: list[tuple[str, bool]] = []

        async def fake_get_mcp_tools(server: str, *, read_only: bool = True):
            calls.append((server, read_only))
            return list(tools)

        monkeypatch.setattr(mcp_client, "get_mcp_tools", fake_get_mcp_tools)
        return calls

    return _install


class TestToolLoading:
    async def test_only_read_only_tools_are_requested(self, patched_tools):
        calls = patched_tools([FakeTool("web_search")])
        llm = ScriptedLLM(sequence=[_final_response()])
        agent = ToolLoopAgent(llm=llm)

        result = await agent.run(_input())

        assert result.ok is True
        assert calls == [("research", True)]

    async def test_tool_schema_strips_tenant_headers(self, patched_tools):
        patched_tools([FakeTool("web_search")])
        agent = ToolLoopAgent(llm=ScriptedLLM(sequence=[_final_response()]))

        schemas, index = await agent._load_tools()

        # The model receives the tool but cannot set tenant headers itself.
        schema = schemas[0]["function"]
        assert schema["name"] == "web_search"
        assert "headers" not in schema["parameters"]["properties"]
        assert "headers" not in schema["parameters"].get("required", [])
        assert "web_search" in index

    async def test_server_failure_is_skipped(self, monkeypatch):
        async def broken(server: str, *, read_only: bool = True):
            raise RuntimeError("mcp offline")

        monkeypatch.setattr(mcp_client, "get_mcp_tools", broken)
        agent = ToolLoopAgent(llm=ScriptedLLM(sequence=[_final_response()]))

        schemas, index = await agent._load_tools()

        assert schemas == []
        assert index == {}


class TestToolExecution:
    async def test_tool_is_executed_with_injected_headers(self, patched_tools):
        tool = FakeTool("web_search", result={"results": [{"title": "A"}]})
        patched_tools([tool])
        llm = ScriptedLLM(
            sequence=[
                _tool_response("web_search", {"query": "project management"}),
                _final_response(),
            ]
        )
        agent = ToolLoopAgent(llm=llm)

        raw = _input()
        result = await agent.run(raw)

        assert result.ok is True
        assert len(tool.calls) == 1
        assert tool.calls[0]["query"] == "project management"
        assert tool.calls[0]["headers"] == {
            "X-AdForge-Tenant": "tenant_a",
            "X-AdForge-Campaign": raw["campaign_id"],
        }

        # The tool result is fed back to the model as a tool message.
        second_call = llm.calls[1]
        tool_messages = [m for m in second_call if m.get("role") == "tool"]
        assert len(tool_messages) == 1
        assert tool_messages[0]["tool_call_id"] == "call_1"
        assert tool_messages[0]["name"] == "web_search"
        assert json.loads(tool_messages[0]["content"]) == {"results": [{"title": "A"}]}

    async def test_usage_is_aggregated_across_rounds(self, patched_tools):
        patched_tools([FakeTool("web_search")])
        llm = ScriptedLLM(
            sequence=[
                _tool_response("web_search", {"query": "x"}),
                _final_response(),
            ]
        )
        agent = ToolLoopAgent(llm=llm)

        result = await agent.run(_input())

        assert result.output.usage["total_tokens"] == 27
        assert result.output.usage["prompt_tokens"] == 20
        assert result.output.usage["completion_tokens"] == 7

    async def test_write_tool_name_is_refused(self, patched_tools):
        # The write tool is not in the index because only read-only tools are
        # loaded. A model that hallucinates its name gets an explicit refusal.
        patched_tools([FakeTool("web_search")])
        llm = ScriptedLLM(
            sequence=[
                _tool_response(
                    "publish_campaign",
                    {"channel": "google", "budget_usd": 1000},
                ),
                _final_response(),
            ]
        )
        agent = ToolLoopAgent(llm=llm)

        result = await agent.run(_input())

        assert result.ok is True
        second_call = llm.calls[1]
        tool_message = next(m for m in second_call if m.get("role") == "tool")
        payload = json.loads(tool_message["content"])
        assert payload["error"] == "tool_not_available"
        assert payload["tool"] == "publish_campaign"
        assert "Only read-only tools" in payload["detail"]

    async def test_tool_failure_is_reported_to_the_model(self, patched_tools):
        class BrokenTool(FakeTool):
            async def ainvoke(self, arguments):
                raise RuntimeError("upstream 503")

        patched_tools([BrokenTool("web_search")])
        llm = ScriptedLLM(
            sequence=[
                _tool_response("web_search", {"query": "x"}),
                _final_response(),
            ]
        )
        agent = ToolLoopAgent(llm=llm)

        result = await agent.run(_input())

        assert result.ok is True
        tool_message = next(m for m in llm.calls[1] if m.get("role") == "tool")
        payload = json.loads(tool_message["content"])
        assert payload["error"] == "tool_call_failed"
        assert "upstream 503" in payload["detail"]

    async def test_large_tool_results_are_truncated(self, patched_tools, monkeypatch):
        from adforge.config import get_settings

        monkeypatch.setenv("AGENT_MAX_TOOL_RESULT_CHARS", "512")
        get_settings.cache_clear()
        big = FakeTool("web_search", result="x" * 5000)
        patched_tools([big])
        llm = ScriptedLLM(
            sequence=[
                _tool_response("web_search", {"query": "x"}),
                _final_response(),
            ]
        )
        agent = ToolLoopAgent(llm=llm)

        await agent.run(_input())

        tool_message = next(m for m in llm.calls[1] if m.get("role") == "tool")
        assert tool_message["content"].endswith("...[truncated]")
        assert len(tool_message["content"]) < 600

    async def test_iteration_limit_fails_closed(self, patched_tools, monkeypatch):
        from adforge.config import get_settings

        monkeypatch.setenv("AGENT_MAX_TOOL_ITERATIONS", "2")
        get_settings.cache_clear()
        patched_tools([FakeTool("web_search")])
        llm = ScriptedLLM(
            sequence=[
                _tool_response("web_search", {"query": "1"}),
                _tool_response("web_search", {"query": "2"}),
                _tool_response("web_search", {"query": "3"}),
            ]
        )
        agent = ToolLoopAgent(llm=llm)

        result = await agent.run(_input())

        assert result.ok is False
        assert "exceeded the read-only tool-call limit (2 iterations)" in (result.output.error)
        assert len(llm.calls) == 2

    async def test_empty_model_reply_gets_one_nudge_then_fails(self, patched_tools, monkeypatch):
        from adforge.config import get_settings

        monkeypatch.setenv("AGENT_MAX_TOOL_ITERATIONS", "2")
        get_settings.cache_clear()
        patched_tools([FakeTool("web_search")])
        llm = ScriptedLLM(
            sequence=[
                LLMResponse(content="", usage=LLMUsage(total_tokens=1)),
                LLMResponse(content="", usage=LLMUsage(total_tokens=1)),
            ]
        )
        agent = ToolLoopAgent(llm=llm)

        result = await agent.run(_input())

        # Two empty replies exhaust the bounded loop rather than spinning.
        assert result.ok is False
        assert "exceeded the read-only tool-call limit" in result.output.error
        assert len(llm.calls) == 2
        nudge = llm.calls[1][-1]
        assert nudge["role"] == "user"
        assert "Return the final JSON object" in nudge["content"]


#: Every read-only tool the first-party MCP servers expose to an agent.
READ_ONLY_TOOL_NAMES = [
    # research
    "web_search",
    "wikipedia_search",
    "serp_scrape",
    "keyword_clusters",
    "competitor_creatives",
    # ads (read only)
    "list_channels",
    # analytics (read only)
    "query_kpis",
    "aggregate_kpis",
    # roi
    "compute_roas",
    "compute_cpa",
    "compute_payback_period",
    "classify_recommendation",
    # compliance
    "evaluate_creative",
    "list_blocked_terms",
    "list_required_disclosures",
    # linkedin ads
    "get_ad_analytics",
    # public data
    "similarweb_traffic",
    "appstore_search",
    "appstore_reviews",
    "ipinfo_lookup",
    "abuseipdb_check",
    "github_repo_stats",
    "hackernews_top",
    "reddit_search",
    "stackexchange_search",
    "producthunt_search",
    "opencorporates_search",
    "wikipedia_search_direct",
]


class TestEveryReadOnlyToolIsCallable:
    @pytest.mark.parametrize("tool_name", READ_ONLY_TOOL_NAMES)
    async def test_tool_can_be_invoked_through_the_loop(self, tool_name, patched_tools):
        tool = FakeTool(tool_name, result={"tool": tool_name, "rows": []})
        patched_tools([tool])
        llm = ScriptedLLM(
            sequence=[
                _tool_response(tool_name, {"query": "smoke"}),
                _final_response(),
            ]
        )
        agent = ToolLoopAgent(llm=llm)

        result = await agent.run(_input())

        assert result.ok is True, result.output.error
        assert tool.calls and tool.calls[0]["query"] == "smoke"
        tool_message = next(m for m in llm.calls[1] if m.get("role") == "tool")
        assert json.loads(tool_message["content"])["tool"] == tool_name
