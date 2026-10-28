"""Tests for the LLM provider factory + adapters.

We don't hit real APIs — just verify the factory dispatches correctly and
that missing keys raise GuardrailError.
"""
from __future__ import annotations

import pytest


class TestLLMFactory:
    def test_factory_returns_glm_provider_by_default(self, monkeypatch):
        # Force fresh cache
        from adforge.llm.factory import get_llm
        get_llm.cache_clear()

        from adforge.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("LLM_PROVIDER", "glm")
        monkeypatch.setenv("ZAI_API_KEY", "fake-key")

        llm = get_llm()
        from adforge.llm.zai_glm import ZaiGLMProvider
        assert isinstance(llm, ZaiGLMProvider)

    def test_factory_dispatches_openai(self, monkeypatch):
        from adforge.llm.factory import get_llm
        get_llm.cache_clear()
        from adforge.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key")

        llm = get_llm()
        from adforge.llm.openai_provider import OpenAIProvider
        assert isinstance(llm, OpenAIProvider)

    def test_factory_dispatches_anthropic(self, monkeypatch):
        from adforge.llm.factory import get_llm
        get_llm.cache_clear()
        from adforge.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

        llm = get_llm()
        from adforge.llm.anthropic_provider import AnthropicProvider
        assert isinstance(llm, AnthropicProvider)

    def test_glm_provider_requires_api_key(self, monkeypatch):
        from adforge.llm.factory import get_llm
        get_llm.cache_clear()
        from adforge.config import get_settings
        get_settings.cache_clear()
        monkeypatch.setenv("LLM_PROVIDER", "glm")
        monkeypatch.delenv("ZAI_API_KEY", raising=False)

        from adforge.guardrails.exceptions import GuardrailError
        with pytest.raises(GuardrailError):
            get_llm()
