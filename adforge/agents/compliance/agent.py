"""Compliance agent node — uses PolicyEnforcer + LLM for nuanced review."""
from __future__ import annotations

import json
from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.compliance.persona import PERSONA
from adforge.agents.parsing import extract_json, schema_instruction, validate_payload
from adforge.guardrails.contracts import AgentInput, ComplianceVerdict
from adforge.guardrails.policy import PolicyEnforcer


class ComplianceAgent(BaseAgent):
    name = "compliance"
    persona = PERSONA
    mcp_servers = ("compliance",)

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
                    "the nuanced issues (misleading framing, platform T&C edge cases). "
                    "Use risk_level='critical' ONLY for a Tier-1 violation or "
                    "sensitive-attribute targeting. Missing/incomplete plan data "
                    "is a 'high' risk review issue, not 'critical'."
                    f"{schema_instruction(ComplianceVerdict)}"
                ),
            },
        ]

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        data = extract_json(content)
        llm_verdict = validate_payload(ComplianceVerdict, data, self.name)

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
            approved=llm_verdict["approved"] and deterministic_verdict.approved,
            risk_level=max(
                llm_verdict["risk_level"],
                deterministic_verdict.risk_level,
                key=["low", "medium", "high", "critical"].index,
            ),
            flagged_issues=list(set(
                llm_verdict["flagged_issues"] + deterministic_verdict.flagged_issues
            )),
            required_disclosures=list(set(
                llm_verdict["required_disclosures"] + deterministic_verdict.required_disclosures
            )),
            reviewer_notes=(
                llm_verdict["reviewer_notes"]
                + " | PolicyEnforcer: "
                + deterministic_verdict.reviewer_notes
            ),
            blocked_terms=deterministic_verdict.blocked_terms,
        )
        return merged.model_dump()
