"""Unit tests for BaseAgent: retries, repair, error mapping, persistence."""

from __future__ import annotations

from uuid import uuid4

from support import VALID_RESEARCH, FailingLLM, ScriptedLLM

from adforge.agents.base import AgentResult, BaseAgent
from adforge.agents.research.agent import ResearchAgent
from adforge.guardrails.contracts import AgentInput
from adforge.guardrails.exceptions import (
    GuardrailError,
    LLMQuotaExceededError,
    LLMTransportError,
)


def _input(**overrides):
    return {
        "request_id": str(uuid4()),
        "tenant_id": "tenant_a",
        "campaign_id": str(uuid4()),
        "agent_name": "research",
        "upstream_payloads": {},
        **overrides,
    }


class BoomAgent(BaseAgent):
    name = "boom"
    persona = "boom"

    def build_messages(self, agent_input: AgentInput):
        raise RuntimeError("kaboom")

    def parse_response(self, content: str, agent_input: AgentInput):
        return {}


class TestInputValidation:
    async def test_missing_tenant_returns_error_output(self):
        agent = ResearchAgent(llm=ScriptedLLM(default=VALID_RESEARCH))
        result = await agent.run(_input(tenant_id=""))
        assert result.ok is False
        assert "input validation failed" in result.output.error
        assert result.output.tenant_id == ""

    async def test_missing_campaign_id_returns_zero_uuid(self):
        raw = _input()
        del raw["campaign_id"]
        agent = ResearchAgent(llm=ScriptedLLM(default=VALID_RESEARCH))
        result = await agent.run(raw)
        assert result.ok is False
        assert result.output.campaign_id.int == 0

    async def test_extra_field_is_rejected(self):
        agent = ResearchAgent(llm=ScriptedLLM(default=VALID_RESEARCH))
        result = await agent.run(_input(unexpected="x"))
        assert result.ok is False


class TestSuccessPath:
    async def test_successful_run_returns_typed_payload_and_usage(self):
        llm = ScriptedLLM(default=VALID_RESEARCH)
        agent = ResearchAgent(llm=llm)
        result = await agent.run(_input())
        assert isinstance(result, AgentResult)
        assert result.ok is True
        assert result.output.payload["market_summary"] == VALID_RESEARCH["market_summary"]
        assert result.output.usage["total_tokens"] == 300
        assert len(llm.calls) == 1

    async def test_lazy_llm_resolution_uses_factory(self, monkeypatch):
        llm = ScriptedLLM(default=VALID_RESEARCH)
        monkeypatch.setattr("adforge.agents.base.get_llm", lambda: llm)
        agent = ResearchAgent()
        result = await agent.run(_input())
        assert result.ok is True
        assert agent._llm is llm

    async def test_summary_never_includes_full_brief(self):
        agent = ResearchAgent(llm=ScriptedLLM(default=VALID_RESEARCH))
        summary = agent._summarise(
            AgentInput(**_input(upstream_payloads={"creative": {"headline": "x"}}))
        )
        assert summary == {
            "agent": "research",
            "brief_present": False,
            "upstream_payload_keys": ["creative"],
        }


class TestRetries:
    async def test_transport_errors_retry_then_succeed(self):
        llm = FailingLLM(LLMTransportError("429"), fail_times=2, then=VALID_RESEARCH)
        agent = ResearchAgent(llm=llm)
        result = await agent.run(_input())
        assert result.ok is True
        assert len(llm.calls) == 3

    async def test_transport_errors_exhaust_budget(self):
        llm = FailingLLM(LLMTransportError("503 overloaded"))
        agent = ResearchAgent(llm=llm)
        result = await agent.run(_input())
        assert result.ok is False
        assert "503 overloaded" in result.output.error
        assert len(llm.calls) == 3

    async def test_non_transport_guardrail_error_is_not_retried(self):
        llm = FailingLLM(GuardrailError("policy says no"))
        agent = ResearchAgent(llm=llm)
        result = await agent.run(_input())
        assert result.ok is False
        assert "policy says no" in result.output.error
        assert len(llm.calls) == 1

    async def test_exhausted_quota_is_not_retried(self):
        llm = FailingLLM(LLMQuotaExceededError("HTTP 429 quota", retry_after_seconds=60))
        agent = ResearchAgent(llm=llm)
        result = await agent.run(_input())
        assert result.ok is False
        assert "HTTP 429 quota" in result.output.error
        assert len(llm.calls) == 1


class TestParseRepair:
    async def test_one_repair_round_recovers(self):
        llm = ScriptedLLM(sequence=["not json at all", VALID_RESEARCH])
        agent = ResearchAgent(llm=llm)
        result = await agent.run(_input())
        assert result.ok is True
        assert len(llm.calls) == 2
        repair_prompt = llm.calls[1][-1]["content"]
        assert "Return ONLY one JSON object" in repair_prompt
        assert "Validation errors" in repair_prompt

    async def test_second_parse_failure_surfaces_contract_error(self):
        llm = ScriptedLLM(sequence=["still bad", "also bad"])
        agent = ResearchAgent(llm=llm)
        result = await agent.run(_input())
        assert result.ok is False
        assert "contract validation failed" in result.output.error
        assert len(llm.calls) == 2

    async def test_semantically_invalid_json_triggers_repair(self):
        llm = ScriptedLLM(sequence=['{"market_summary": "short"}', VALID_RESEARCH])
        agent = ResearchAgent(llm=llm)
        result = await agent.run(_input())
        assert result.ok is True
        assert len(llm.calls) == 2


class TestErrorMappingAndPersistence:
    async def test_unexpected_error_is_mapped(self):
        agent = BoomAgent(llm=ScriptedLLM(default={}))
        result = await agent.run({**_input(), "agent_name": "boom"})
        assert result.ok is False
        assert result.output.error.startswith("unexpected error: RuntimeError")

    async def test_audit_failure_never_fails_the_run(self, monkeypatch):
        class BrokenAudit:
            async def record(self, event):
                raise RuntimeError("audit db down")

        agent = ResearchAgent(llm=ScriptedLLM(default=VALID_RESEARCH))
        agent._audit = BrokenAudit()
        result = await agent.run(_input())
        assert result.ok is True

    async def test_persistence_failure_never_fails_the_run(self, monkeypatch):
        def broken_factory():
            raise RuntimeError("db down")

        monkeypatch.setattr("adforge.storage.session.get_session_factory", broken_factory)
        agent = ResearchAgent(llm=ScriptedLLM(default=VALID_RESEARCH))
        result = await agent.run(_input())
        assert result.ok is True
