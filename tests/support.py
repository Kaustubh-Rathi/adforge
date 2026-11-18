"""Shared test doubles and fixtures for AdForge's test suite."""

from __future__ import annotations

import json
from typing import Any

from adforge.llm.base import LLMProvider, LLMResponse, LLMUsage

# --------------------------------------------------------------------------- #
# Schema-valid payloads keyed by the schema name used in each agent prompt
# --------------------------------------------------------------------------- #

RESEARCH_PAYLOAD: dict[str, Any] = {
    "market_summary": (
        "B2B SaaS project management is a $30B market growing 12% YoY. "
        "Remote teams prioritise integrations and fast onboarding."
    ),
    "top_keywords": [
        "project management for dev teams",
        "jira alternative",
        "sprint planning tool",
    ],
    "competitor_creatives": [
        {"brand": "Linear", "headline": "Plan less, ship more"},
        {"brand": "Asana", "headline": "Work management, simplified"},
    ],
    "audience_persona": {
        "title": "Engineering Manager",
        "pain_points": ["Too many tools", "Slow standups"],
    },
    "trends": [{"name": "AI standup summaries", "momentum": "high"}],
    "sources": ["https://example.com/pm-market"],
}

CREATIVE_PAYLOAD: dict[str, Any] = {
    "headline": "Ship faster with calmer sprints",
    "primary_text": (
        "Acme PM gives dev teams instant burndowns and AI standup summaries. "
        "Set up in five minutes."
    ),
    "cta": "Start free",
    "visual_direction": (
        "Split screen: chaotic spreadsheet on the left, calm Acme board on the right."
    ),
    "ab_variants": [{"headline": "Less status, more shipping"}],
    "channel_adaptations": {
        "google": {"headline": "Acme PM for dev teams"},
        "linkedin": {"headline": "Calmer sprints start here"},
    },
}

MEDIA_PLAN_PAYLOAD: dict[str, Any] = {
    "channel_allocations": [
        {
            "channel": "google",
            "budget_usd": 15000.0,
            "expected_cpc": 3.5,
            "expected_cvr": 0.04,
        },
        {
            "channel": "linkedin",
            "budget_usd": 10000.0,
            "expected_cpc": 8.0,
            "expected_cvr": 0.06,
        },
    ],
    "total_budget_usd": 25000.0,
    "bidding_strategy": "max_conversions",
    "audience_targeting": {"age": "28-45", "industries": ["SaaS"]},
    "expected_reach": 120000,
    "expected_impressions": 480000,
    "flight_dates": {"start": "2026-02-01", "end": "2026-03-02"},
}

COMPLIANCE_PAYLOAD: dict[str, Any] = {
    "approved": True,
    "risk_level": "low",
    "flagged_issues": [],
    "required_disclosures": [],
    "reviewer_notes": "No prohibited terms; audience targeting is compliant.",
    "blocked_terms": [],
}

ROI_PAYLOAD: dict[str, Any] = {
    "total_spend_usd": 4500.0,
    "total_revenue_usd": 13500.0,
    "total_conversions": 38,
    "blended_roas": 3.0,
    "blended_cpa": 118.42,
    "payback_period_days": 45.0,
    "lifetime_value_usd": 600.0,
    "net_profit_usd": 9000.0,
    "recommendation": "optimize",
    "rationale": (
        "Google drove 85% of conversions at 4.1x ROAS while LinkedIn lagged "
        "at 1.2x; shift budget toward Google."
    ),
    "per_channel_breakdown": [{"channel": "google", "roas": 4.1}],
}

OPTIMIZATION_PAYLOAD: dict[str, Any] = {
    "suggestions": [
        {
            "channel": "google",
            "action": "increase_budget",
            "delta_pct": 0.3,
            "rationale": "ROAS 4.1x with CPA below target; scale the winner.",
            "expected_impact": {"cpa": "-8%", "roas": "+15%"},
        },
        {
            "channel": "linkedin",
            "action": "decrease_budget",
            "delta_pct": -0.5,
            "rationale": "ROAS 1.2x and CPA three times target; cut spend.",
            "expected_impact": {"cpa": "+20%", "spend": "-50%"},
        },
    ]
}

# Short aliases used by the focused unit suites.
VALID_RESEARCH = RESEARCH_PAYLOAD
VALID_CREATIVE = CREATIVE_PAYLOAD
VALID_MEDIA_PLAN = MEDIA_PLAN_PAYLOAD
VALID_COMPLIANCE = COMPLIANCE_PAYLOAD
VALID_ROI = ROI_PAYLOAD
VALID_OPTIMIZATION = OPTIMIZATION_PAYLOAD


def default_agent_responses() -> dict[str, Any]:
    """Map a prompt needle → schema-valid payload for every LLM-backed agent.

    The needles are the exact instruction sentences from each agent's user
    prompt (not bare schema names, which also appear in personas).
    """
    return {
        "Produce a ResearchFindings JSON object": RESEARCH_PAYLOAD,
        "Produce a CreativeConcept JSON object": CREATIVE_PAYLOAD,
        "Produce a MediaPlan JSON object": MEDIA_PLAN_PAYLOAD,
        "Produce a ComplianceVerdict JSON object": COMPLIANCE_PAYLOAD,
        "Produce an RoiReport JSON object": ROI_PAYLOAD,
        "OptimizationSuggestion, ...": OPTIMIZATION_PAYLOAD,
    }


# --------------------------------------------------------------------------- #
# Scripted provider
# --------------------------------------------------------------------------- #


class ScriptedLLM(LLMProvider):
    """Deterministic provider.

    Two modes:

    * ``responses`` — map a substring of the prompt to a payload/response.
      Useful when the agent under test is known by its prompt.
    * ``sequence`` — pop responses in order. Use this to model retries and the
      JSON parse-repair round.
    """

    def __init__(
        self,
        responses: dict[str, Any] | None = None,
        *,
        sequence: list[Any] | None = None,
        default: Any = None,
        usage: LLMUsage | None = None,
    ) -> None:
        self.responses = responses or {}
        self.sequence = list(sequence) if sequence is not None else None
        self.default = default
        self.usage = usage or LLMUsage(prompt_tokens=100, completion_tokens=200, total_tokens=300)
        self.calls: list[list[dict[str, str]]] = []

    @staticmethod
    def _as_response(item: Any, usage: LLMUsage) -> LLMResponse:
        if isinstance(item, LLMResponse):
            return item
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            return LLMResponse(content=item, usage=usage)
        return LLMResponse(content=json.dumps(item), usage=usage)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])

        if self.sequence is not None:
            if not self.sequence:
                raise AssertionError("ScriptedLLM sequence exhausted")
            return self._as_response(self.sequence.pop(0), self.usage)

        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        for needle, payload in self.responses.items():
            if needle in prompt:
                if callable(payload):
                    payload = payload(messages)
                return self._as_response(payload, self.usage)

        if self.default is None:
            raise AssertionError(f"ScriptedLLM has no response matching prompt: {prompt[:200]!r}")
        return self._as_response(self.default, self.usage)


class FailingLLM(LLMProvider):
    """Always raises the supplied exception (optionally N times, then succeeds)."""

    def __init__(
        self,
        error: BaseException,
        *,
        fail_times: int | None = None,
        then: Any = None,
    ) -> None:
        self.error = error
        self.fail_times = fail_times
        self.then_response = then
        self.calls: list[list[dict[str, str]]] = []

    async def chat(self, messages, **kwargs: Any) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        if self.fail_times is None or len(self.calls) <= self.fail_times:
            raise self.error
        return ScriptedLLM._as_response(
            self.then_response, LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        )
