"""OpenAI adapter — swap in by setting LLM_PROVIDER=openai."""
from __future__ import annotations

import json
from typing import Any

from adforge.config import get_settings
from adforge.guardrails.exceptions import GuardrailError
from adforge.llm.base import LLMProvider, LLMResponse, LLMUsage, ToolCall


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.openai_api_key
        self.model = model or s.openai_model
        if not self.api_key:
            raise GuardrailError("OPENAI_API_KEY not set — cannot init OpenAIProvider.")
        # Lazy import so users who never use OpenAI don't pay the install cost.
        from openai import AsyncOpenAI
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=s.openai_base_url,
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
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else s.llm_temperature,
            "max_tokens": max_tokens or s.llm_max_tokens,
        }
        if tools:
            kwargs["tools"] = tools

        resp = await self._client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        usage = LLMUsage(
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
            total_tokens=resp.usage.total_tokens if resp.usage else 0,
        )
        tool_calls: list[ToolCall] = []
        for call in choice.message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except ValueError:
                arguments = {"_raw_arguments": call.function.arguments}
            if not isinstance(arguments, dict):
                arguments = {"value": arguments}
            tool_calls.append(
                ToolCall(id=call.id, name=call.function.name, arguments=arguments)
            )
        return LLMResponse(
            content=choice.message.content or "",
            usage=usage,
            raw=resp.model_dump(),
            finish_reason=choice.finish_reason or "stop",
            tool_calls=tool_calls,
        )
