"""Anthropic adapter — swap in by setting LLM_PROVIDER=anthropic."""

from __future__ import annotations

import json
from typing import Any

from adforge.config import get_settings
from adforge.guardrails.exceptions import GuardrailError
from adforge.llm.base import LLMProvider, LLMResponse, LLMUsage, ToolCall


def _tool_result_content(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    return content if isinstance(content, str) else json.dumps(content, default=str)


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for tool in tools:
        function = tool.get("function") or {}
        if not function.get("name"):
            continue
        converted.append(
            {
                "name": function["name"],
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object"},
            }
        )
    return converted


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.anthropic_api_key
        self.model = model or s.anthropic_model
        if not self.api_key:
            raise GuardrailError("ANTHROPIC_API_KEY not set — cannot init AnthropicProvider.")
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(
            api_key=self.api_key,
            base_url=s.anthropic_base_url,
            timeout=s.llm_timeout,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        s = get_settings()
        # Anthropic separates system from messages and uses content blocks for
        # tool use/results. Translate the OpenAI-shaped loop messages.
        system_msg = ""
        chat_msgs: list[dict[str, Any]] = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                system_msg += str(m.get("content") or "") + "\n"
            elif role == "tool":
                chat_msgs.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.get("tool_call_id", ""),
                                "content": _tool_result_content(m),
                            }
                        ],
                    }
                )
            elif role == "assistant" and m.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": str(m["content"])})
                for call in m["tool_calls"]:
                    function = call.get("function") or {}
                    raw_arguments = function.get("arguments", "{}")
                    try:
                        arguments = (
                            json.loads(raw_arguments)
                            if isinstance(raw_arguments, str)
                            else raw_arguments
                        )
                    except ValueError:
                        arguments = {"_raw_arguments": raw_arguments}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id", ""),
                            "name": function.get("name", ""),
                            "input": arguments if isinstance(arguments, dict) else {},
                        }
                    )
                chat_msgs.append({"role": "assistant", "content": blocks})
            else:
                chat_msgs.append(m)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": chat_msgs,
            "max_tokens": max_tokens or s.llm_max_tokens,
            "temperature": temperature if temperature is not None else s.llm_temperature,
        }
        if system_msg:
            kwargs["system"] = system_msg.strip()
        if tools:
            kwargs["tools"] = _convert_tools(tools)

        resp = await self._client.messages.create(**kwargs)
        content = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            arguments = getattr(block, "input", {})
            tool_calls.append(
                ToolCall(
                    id=str(getattr(block, "id", "")),
                    name=str(getattr(block, "name", "")),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        usage = LLMUsage(
            prompt_tokens=resp.usage.input_tokens,
            completion_tokens=resp.usage.output_tokens,
            total_tokens=resp.usage.input_tokens + resp.usage.output_tokens,
        )
        return LLMResponse(
            content=content,
            usage=usage,
            raw=resp.model_dump(),
            finish_reason=resp.stop_reason or "stop",
            tool_calls=tool_calls,
        )
