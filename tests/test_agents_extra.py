"""Focused unit tests for agent parsing, factory selection, and isolation helpers."""

from __future__ import annotations

import json
from datetime import UTC
from uuid import uuid4

import pytest
from support import OPTIMIZATION_PAYLOAD

from adforge.agents.optimization.agent import OptimizationAgent
from adforge.guardrails.contracts import AgentInput
from adforge.guardrails.data_isolation import (
    TenantScope,
    assert_tenant_access,
    tenant_scope,
)
from adforge.guardrails.exceptions import ContractValidationError, DataIsolationError
from adforge.llm.base import LLMProvider
from adforge.storage.base import utcnow


def _input() -> AgentInput:
    return AgentInput(
        request_id=uuid4(),
        tenant_id="acme",
        campaign_id=uuid4(),
        agent_name="optimization",
        upstream_payloads={},
    )


class _NullLLM(LLMProvider):
    async def chat(self, messages, **kwargs):
        raise AssertionError("not called")


class TestOptimizationParsing:
    def setup_method(self):
        self.agent = OptimizationAgent(llm=_NullLLM())

    def test_valid_suggestions_are_truncated_to_three(self):
        payload = {
            "suggestions": [
                OPTIMIZATION_PAYLOAD["suggestions"][0],
                OPTIMIZATION_PAYLOAD["suggestions"][1],
                OPTIMIZATION_PAYLOAD["suggestions"][0],
                OPTIMIZATION_PAYLOAD["suggestions"][1],
            ]
        }
        parsed = self.agent.parse_response(json.dumps(payload), _input())
        assert len(parsed["suggestions"]) == 3

    def test_non_list_suggestions_raises(self):
        with pytest.raises(ContractValidationError) as exc:
            self.agent.parse_response('{"suggestions": "nope"}', _input())
        assert exc.value.errors[0]["loc"] == ["suggestions"]

    def test_malformed_suggestions_are_skipped(self):
        content = (
            '{"suggestions": ['
            '{"channel": "google", "action": "increase_budget", "delta_pct": 0.2, '
            '"rationale": "ROAS is above target, so scale spend.", '
            '"expected_impact": {"roas": "+1"}},'
            '{"channel": "nope"}'
            "]}"
        )
        parsed = self.agent.parse_response(content, _input())
        assert len(parsed["suggestions"]) == 1
        assert parsed["suggestions"][0]["channel"] == "google"

    def test_all_malformed_raises(self):
        with pytest.raises(ContractValidationError) as exc:
            self.agent.parse_response('{"suggestions": [{"channel": "nope"}]}', _input())
        assert exc.value.errors[0]["msg"] == "no valid suggestion objects"

    def test_empty_suggestions_is_valid(self):
        assert self.agent.parse_response('{"suggestions": []}', _input()) == {"suggestions": []}


class TestFactory:
    def test_opencode_provider_is_selected(self, monkeypatch):
        from adforge.config import get_settings
        from adforge.llm.factory import get_llm
        from adforge.llm.opencode_provider import OpencodeProvider

        monkeypatch.setenv("LLM_PROVIDER", "opencode")
        monkeypatch.setenv("LLM_BASE_URL", "https://opencode.ai/inference/openai/v1")
        monkeypatch.setenv("LLM_MODEL", "test-model")
        monkeypatch.setenv("LLM_API_KEY", "public")
        get_settings.cache_clear()
        get_llm.cache_clear()
        try:
            provider = get_llm()
            assert isinstance(provider, OpencodeProvider)
            assert get_llm() is provider  # cached singleton
        finally:
            get_llm.cache_clear()
            get_settings.cache_clear()


class TestIsolationHelpers:
    def test_assert_matches_accepts_same_tenant(self):
        scope = TenantScope("acme", uuid4())
        scope.assert_matches("acme", "campaign")

    def test_assert_matches_rejects_other_tenant(self):
        scope = TenantScope("acme", uuid4())
        with pytest.raises(DataIsolationError):
            scope.assert_matches("intruder", "campaign")
        with pytest.raises(DataIsolationError):
            assert_tenant_access(scope, "intruder", "campaign")

    def test_headers_and_cache_key(self):
        campaign_id = uuid4()
        scope = TenantScope("acme", campaign_id)
        assert scope.as_headers() == {
            "X-AdForge-Tenant": "acme",
            "X-AdForge-Campaign": str(campaign_id),
        }
        assert scope.cache_key("kpi", "google") == (f"adforge:acme:{campaign_id}:kpi:google")
        assert "tenant='acme'" in str(scope)

    async def test_empty_tenant_is_rejected(self):
        with pytest.raises(DataIsolationError):
            async with tenant_scope("   ", uuid4()):
                pass


class TestUtcNow:
    def test_returns_aware_utc(self):
        now = utcnow()
        assert now.tzinfo is not None
        assert now.utcoffset() == UTC.utcoffset(None)
