"""Optimization Loop agent node."""
from __future__ import annotations

import json
from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.optimization.persona import PERSONA
from adforge.guardrails.contracts import AgentInput, OptimizationSuggestion


class OptimizationAgent(BaseAgent):
    name = "optimization"
    persona = PERSONA

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        kpis = json.dumps(agent_input.upstream_payloads.get("kpi_analytics", {}), indent=2, default=str)
        roi = json.dumps(agent_input.upstream_payloads.get("roi_calculator", {}), indent=2, default=str)
        media_plan = json.dumps(agent_input.upstream_payloads.get("media_plan", {}), indent=2, default=str)

        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": (
                    f"Media plan:\n{media_plan}\n\n"
                    f"Live KPIs:\n{kpis}\n\n"
                    f"ROI report:\n{roi}\n\n"
                    "Produce a JSON object: {\"suggestions\": [OptimizationSuggestion, ...]}. "
                    "Max 3 suggestions. Sum of delta_pct * budget should approximate zero."
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
            data = {"suggestions": [], "_parse_error": str(e)}

        suggestions = []
        for s in data.get("suggestions", [])[:3]:
            try:
                suggestions.append(OptimizationSuggestion(**s).model_dump())
            except Exception:
                # Skip malformed suggestions rather than failing the whole run.
                continue

        return {"suggestions": suggestions}
