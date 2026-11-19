"""Creative Strategist agent node."""

from __future__ import annotations

import json
from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.creative_strategist.persona import PERSONA
from adforge.agents.parsing import extract_json, schema_instruction, validate_payload
from adforge.guardrails.contracts import AgentInput, CreativeConcept


class CreativeStrategistAgent(BaseAgent):
    name = "creative_strategist"
    persona = PERSONA
    mcp_servers = ("research",)

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        brief = agent_input.brief.model_dump_json(indent=2) if agent_input.brief else "{}"
        research = json.dumps(
            agent_input.upstream_payloads.get("research", {}), indent=2, default=str
        )
        reviewer_comments = agent_input.upstream_payloads.get("reviewer_comments")
        revision_note = ""
        if reviewer_comments:
            revision_note = (
                "\n\nA human reviewer rejected the previous concept. "
                f"Address these comments in the new concept:\n{reviewer_comments}\n"
            )
        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": (
                    f"Campaign brief:\n{brief}\n\n"
                    f"Research findings:\n{research}\n\n"
                    "Produce a CreativeConcept JSON object. Include channel "
                    "adaptations for every channel in the brief."
                    f"{revision_note}"
                    f"{schema_instruction(CreativeConcept)}"
                ),
            },
        ]

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        data = extract_json(content)
        return validate_payload(CreativeConcept, data, self.name)
