"""End-to-end proof that the real LLM transport + tool loop work together.

A tiny local OpenAI-compatible server streams SSE tool-call deltas exactly like
a hosted provider. The real :class:`OpencodeProvider` parses those deltas, the
real ``BaseAgent`` loop executes the requested read-only MCP tool, feeds the
result back, and the model returns the final JSON. No external network or
credentials are involved.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest

import adforge.mcp_servers.client as mcp_client
from adforge.agents.base import BaseAgent
from adforge.guardrails.contracts import AgentInput
from adforge.llm.opencode_provider import OpencodeProvider


class _FakeTool:
    name = "compute_roas"
    description = "Compute return on ad spend."
    args_schema = {
        "type": "object",
        "properties": {
            "revenue_usd": {"type": "number"},
            "spend_usd": {"type": "number"},
            "headers": {"type": "object"},
        },
        "required": ["revenue_usd", "spend_usd"],
    }

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def ainvoke(self, arguments: dict[str, Any]) -> Any:
        self.calls.append(arguments)
        return {
            "roas": arguments["revenue_usd"] / arguments["spend_usd"],
            "source": self.name,
        }


class _FakeOpenAIServer(BaseHTTPRequestHandler):
    """Streams one tool call, then a final answer once the tool result arrives."""

    requests: list[dict[str, Any]] = []
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).requests.append(payload)

        messages = payload.get("messages", [])
        tool_result = next((m for m in reversed(messages) if m.get("role") == "tool"), None)
        if tool_result is None:
            events = [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_roas",
                                        "function": {
                                            "name": "compute_roas",
                                            "arguments": json.dumps(
                                                {
                                                    "revenue_usd": 100.0,
                                                    "spend_usd": 25.0,
                                                }
                                            ),
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 30,
                        "completion_tokens": 6,
                        "total_tokens": 36,
                    },
                },
            ]
        else:
            events = [
                {
                    "choices": [
                        {
                            "delta": {
                                "content": json.dumps({"ok": True, "roas": 4.0, "used_tool": True})
                            }
                        }
                    ]
                },
                {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 40,
                        "completion_tokens": 8,
                        "total_tokens": 48,
                    },
                },
            ]

        body = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events)
        body += b"data: [DONE]\n\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def log_message(self, *args: Any) -> None:  # silence the test log
        pass


class _RoundTripAgent(BaseAgent):
    name = "roundtrip"
    persona = "You are a test agent."
    mcp_servers = ("roi",)

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.persona},
            {"role": "user", "content": "Compute ROAS for revenue=100 spend=25."},
        ]

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        return json.loads(content)


@pytest.fixture
def fake_openai_server():
    _FakeOpenAIServer.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenAIServer)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def test_real_transport_executes_read_only_tool_then_answers(fake_openai_server, monkeypatch):
    tool = _FakeTool()

    async def fake_get_mcp_tools(server: str, *, read_only: bool = True):
        assert server == "roi"
        assert read_only is True
        return [tool]

    monkeypatch.setattr(mcp_client, "get_mcp_tools", fake_get_mcp_tools)

    provider = OpencodeProvider(
        base_url=fake_openai_server,
        model="fake-model",
        api_key="fake-key",
        timeout=10,
    )
    agent = _RoundTripAgent(llm=provider)

    result = await agent.run(
        {
            "request_id": str(uuid4()),
            "tenant_id": "tenant_a",
            "campaign_id": str(uuid4()),
            "agent_name": "roundtrip",
            "upstream_payloads": {},
        }
    )

    assert result.ok is True, result.output.error
    assert result.output.payload == {"ok": True, "roas": 4.0, "used_tool": True}
    assert result.output.usage["total_tokens"] == 84

    # The tool ran with the model's arguments plus orchestrator tenant headers.
    assert len(tool.calls) == 1
    assert tool.calls[0]["revenue_usd"] == 100.0
    assert tool.calls[0]["spend_usd"] == 25.0
    assert tool.calls[0]["headers"]["X-AdForge-Tenant"] == "tenant_a"

    # Two real HTTP round trips: tool call, then final answer.
    assert len(_FakeOpenAIServer.requests) == 2
    first, second = _FakeOpenAIServer.requests
    advertised = {tool["function"]["name"] for tool in first.get("tools", [])}
    assert advertised == {"compute_roas"}

    tool_messages = [m for m in second["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_roas"
    assert json.loads(tool_messages[0]["content"])["roas"] == 4.0

    # The model never sees a write tool, even though it asked for one by name.
    assert "publish_campaign" not in advertised
