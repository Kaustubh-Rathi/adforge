"""Anthropic adapter — swap in by setting LLM_PROVIDER=anthropic."""
from __future__ import annotations

from typing import Any

from adforge.config import get_settings
from adforge.llm.base import LLMProvider, LLMResponse, LLMUsage
from adforge.guardrails.exceptions import GuardrailError


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.anthropic_api_key
        self.model = model or s.anthropic_model
        if not self.api_key:
            raise GuardrailError("ANTHROPIC_API_KEY not set — cannot init AnthropicProvider.")
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(api_key=self.api_key)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        s = get_settings()
        # Anthropic separates system from messages.
        system_msg = ""
        chat_msgs = []
        for m in messages:
            if m["role"] == "system":
                system_msg += m["content"] + "\n"
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
            kwargs["tools"] = tools

        resp = await self._client.messages.create(**kwargs)
        content = "".join(b.text for b in resp.content if hasattr(b, "text"))
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
        )
