"""Compliance agent node — uses PolicyEnforcer + LLM for nuanced review."""
from __future__ import annotations

import json
from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.compliance.persona import PERSONA
from adforge.guardrails.contracts import AgentInput, ComplianceVerdict
from adforge.guardrails.policy import PolicyEnforcer


class ComplianceAgent(BaseAgent):
    name = "compliance"
    persona = PERSONA

    def __init__(self) -> None:
        super().__init__()
        self._enforcer = PolicyEnforcer.default()

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        creative = json.dumps(agent_input.upstream_payloads.get("creative", {}), indent=2, default=str)
        media_plan = json.dumps(agent_input.upstream_payloads.get("media_plan", {}), indent=2, default=str)

        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": (
                    f"Creative concept to review:\n{creative}\n\n"
                    f"Media plan:\n{media_plan}\n\n"
                    "Produce a ComplianceVerdict JSON object. The PolicyEnforcer "
                    "has already run on prohibited terms; your job is to catch "
                    "the nuanced issues (misleading framing, platform T&C edge cases)."
                ),
            },
        ]

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        body = content.strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[1]
            if body.endswith("```"):
                body = body.rsplit("```", 1)[0]
        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            data = {
                "approved": False,
                "risk_level": "high",
                "flagged_issues": [f"LLM output not parseable: {e}"],
                "required_disclosures": [],
                "reviewer_notes": "Failed to parse LLM verdict — defaulting to NOT approved.",
                "blocked_terms": [],
            }

        # Re-run the deterministic PolicyEnforcer as a hard gate.
        creative = agent_input.upstream_payloads.get("creative", {})
        media_plan = agent_input.upstream_payloads.get("media_plan", {})
        creative_text = " ".join([
            str(creative.get("headline", "")),
            str(creative.get("primary_text", "")),
            str(creative.get("cta", "")),
        ])
        deterministic_verdict = self._enforcer.evaluate(
            creative_text,
            audience_targeting=media_plan.get("audience_targeting"),
        )

        # Merge: LLM verdict takes the lead, but if PolicyEnforcer hard-blocks,
        # we override to critical+not-approved.
        merged = ComplianceVerdict(
            approved=data.get("approved", False) and deterministic_verdict.approved,
            risk_level=max(
                data.get("risk_level", "medium"),
                deterministic_verdict.risk_level,
                key=["low", "medium", "high", "critical"].index,
            ),
            flagged_issues=list(set(
                data.get("flagged_issues", []) + deterministic_verdict.flagged_issues
            )),
            required_disclosures=list(set(
                data.get("required_disclosures", []) + deterministic_verdict.required_disclosures
            )),
            reviewer_notes=data.get("reviewer_notes", "") + " | PolicyEnforcer: " + deterministic_verdict.reviewer_notes,
            blocked_terms=deterministic_verdict.blocked_terms,
        )
        return merged.model_dump()
