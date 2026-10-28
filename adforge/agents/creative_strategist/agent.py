"""Creative Strategist agent node."""
from __future__ import annotations

import json
from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.creative_strategist.persona import PERSONA
from adforge.guardrails.contracts import AgentInput, CreativeConcept


class CreativeStrategistAgent(BaseAgent):
    name = "creative_strategist"
    persona = PERSONA

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        brief = agent_input.brief.model_dump_json(indent=2) if agent_input.brief else "{}"
        research = json.dumps(agent_input.upstream_payloads.get("research", {}), indent=2, default=str)
        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": (
                    f"Campaign brief:\n{brief}\n\n"
                    f"Research findings:\n{research}\n\n"
                    "Produce a CreativeConcept JSON object. Include channel "
                    "adaptations for every channel in the brief."
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
                "headline": "Untitled campaign",
                "primary_text": body[:500],
                "cta": "Learn more",
                "visual_direction": body[:1000],
                "_parse_error": str(e),
            }
        concept = CreativeConcept(**data)
        return concept.model_dump()
