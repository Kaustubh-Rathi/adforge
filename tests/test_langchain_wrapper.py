"""Tests for the LangChain-compatible wrapper around LLMProvider."""
from __future__ import annotations

import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel
from support import VALID_RESEARCH, ScriptedLLM

from adforge.llm.base import LLMResponse
from adforge.llm.langchain_wrapper import LangChainLLMWrapper


class RecordingLLM(ScriptedLLM):
    """Scripted provider that also records the tool schemas it receives."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tool_snapshots: list = []

    async def chat(
        self,
        messages,
        *,
        temperature=None,
        max_tokens=None,
        tools=None,
    ):
        self.tool_snapshots.append(tools)
        return await super().chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
        )


class SearchArgs(BaseModel):
    query: str
    limit: int = 5


async def test_agenerate_converts_langchain_messages():
    llm = ScriptedLLM(default=VALID_RESEARCH)
    wrapper = LangChainLLMWrapper(provider=llm)

    result = await wrapper._agenerate_internal([
        SystemMessage(content="system prompt"),
        HumanMessage(content="user prompt"),
        AIMessage(content="assistant reply"),
        ToolMessage(content="tool output", tool_call_id="call-1"),
    ])

    assert llm.calls == [[
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
        {"role": "assistant", "content": "assistant reply"},
        {"role": "user", "content": "tool output"},
    ]]
    message = result.generations[0].message
    assert isinstance(message, AIMessage)
    assert json.loads(message.content) == VALID_RESEARCH


async def test_tool_calls_are_copied_to_additional_kwargs():
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "web_search", "arguments": '{"query": "crm"}'},
        }
    ]
    llm = ScriptedLLM(default=LLMResponse(content="", raw={"tool_calls": tool_calls}))
    wrapper = LangChainLLMWrapper(provider=llm)

    result = await wrapper._agenerate_internal([HumanMessage(content="find leads")])

    message = result.generations[0].message
    assert isinstance(message, AIMessage)
    assert message.content == ""
    assert message.additional_kwargs["tool_calls"] == tool_calls


async def test_bind_tools_builds_schemas_and_passes_them_to_provider():
    llm = RecordingLLM(default=VALID_RESEARCH)
    wrapper = LangChainLLMWrapper(provider=llm)
    raw_schema = {
        "type": "function",
        "function": {
            "name": "raw_tool",
            "description": "Already in provider format",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    tool = SimpleNamespace(
        name="web_search",
        description="Search the public web",
        args_schema=SearchArgs,
    )

    bound = wrapper.bind_tools([tool, raw_schema])

    assert bound is not wrapper
    assert bound._bound_tools == [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the public web",
                "parameters": SearchArgs.model_json_schema(),
            },
        },
        raw_schema,
    ]
    assert wrapper._bound_tools == []

    await bound._agenerate_internal([HumanMessage(content="find crm tools")])

    assert llm.tool_snapshots == [bound._bound_tools]


def test_generate_sync_proxy_works_outside_event_loop():
    llm = ScriptedLLM(default=VALID_RESEARCH)
    wrapper = LangChainLLMWrapper(provider=llm)

    result = wrapper._generate([HumanMessage(content="hi")])

    message = result.generations[0].message
    assert isinstance(message, AIMessage)
    assert json.loads(message.content) == VALID_RESEARCH


def test_llm_type_property():
    wrapper = LangChainLLMWrapper(provider=ScriptedLLM(default={}))
    assert wrapper._llm_type == "adforge-llm-wrapper"
