"""LLM provider base class + shared types."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMUsage:
    """Token usage accounting, normalised across providers."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class LLMResponse:
    """Normalised LLM response. Agents consume .content; BaseAgent logs .usage."""

    content: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = "stop"


class LLMProvider(ABC):
    """Abstract base — every adapter implements this."""

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Send a chat completion. ``messages`` follows OpenAI shape:

            [{"role": "system", "content": "..."},
             {"role": "user",   "content": "..."}]

        Returns a normalised LLMResponse.
        """
        ...
