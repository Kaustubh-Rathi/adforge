"""LLM factory — returns the configured provider (cached)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from adforge.config import get_settings
from adforge.llm.base import LLMProvider


@lru_cache
def get_llm() -> LLMProvider:
    """Return a cached LLMProvider instance per the configured provider.

    Settings are loaded at first call; tests can clear the cache via
    ``get_llm.cache_clear()`` after monkeypatching settings.
    """
    settings = get_settings()
    provider: Literal["glm", "openai", "anthropic"] = settings.llm_provider

    if provider == "glm":
        from adforge.llm.zai_glm import ZaiGLMProvider
        return ZaiGLMProvider()
    elif provider == "openai":
        from adforge.llm.openai_provider import OpenAIProvider
        return OpenAIProvider()
    elif provider == "anthropic":
        from adforge.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider()
    else:  # pragma: no cover
        raise ValueError(f"Unknown LLM provider: {provider}")
