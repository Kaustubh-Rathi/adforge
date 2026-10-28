"""Z.ai GLM adapter (default LLM provider)."""
from __future__ import annotations

import os
from typing import Any

import httpx

from adforge.config import get_settings
from adforge.llm.base import LLMProvider, LLMResponse, LLMUsage
from adforge.guardrails.exceptions import GuardrailError


class ZaiGLMProvider(LLMProvider):
    """Adapter for Z.ai GLM-4.6 (and other Z.ai models).

    Uses the OpenAI-compatible chat completions endpoint at
    https://open.bigmodel.cn/api/paas/v4/chat/completions so we don't need
    a Z.ai-specific SDK installed.
    """

    BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        s = get_settings()
        self.api_key = api_key or s.zai_api_key
        self.model = model or s.zai_model
        if not self.api_key:
            raise GuardrailError(
                "ZAI_API_KEY not set — cannot init ZaiGLMProvider. "
                "Either set ZAI_API_KEY in .env or change LLM_PROVIDER."
            )

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        s = get_settings()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else s.llm_temperature,
            "max_tokens": max_tokens or s.llm_max_tokens,
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{self.BASE_URL}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        usage_raw = data.get("usage", {})
        usage = LLMUsage(
            prompt_tokens=usage_raw.get("prompt_tokens", 0),
            completion_tokens=usage_raw.get("completion_tokens", 0),
            total_tokens=usage_raw.get("total_tokens", 0),
            # Rough cost estimate; refine as Z.ai publishes pricing.
            cost_usd=usage_raw.get("total_tokens", 0) * 0.000002,
        )
        return LLMResponse(
            content=choice["message"]["content"],
            usage=usage,
            raw=data,
            finish_reason=choice.get("finish_reason", "stop"),
        )
