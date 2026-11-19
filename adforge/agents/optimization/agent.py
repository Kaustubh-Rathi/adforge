"""Optimization Loop agent node."""

from __future__ import annotations

import json
from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.optimization.persona import PERSONA
from adforge.agents.parsing import extract_json, schema_instruction, validate_payload
from adforge.guardrails.contracts import AgentInput, OptimizationSuggestion
from adforge.guardrails.exceptions import ContractValidationError


class OptimizationAgent(BaseAgent):
    name = "optimization"
    persona = PERSONA
    mcp_servers = ("analytics", "roi")

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        kpis = json.dumps(
            agent_input.upstream_payloads.get("kpi_analytics", {}), indent=2, default=str
        )
        roi = json.dumps(
            agent_input.upstream_payloads.get("roi_calculator", {}), indent=2, default=str
        )
        media_plan = json.dumps(
            agent_input.upstream_payloads.get("media_plan", {}), indent=2, default=str
        )

        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": (
                    f"Media plan:\n{media_plan}\n\n"
                    f"Live KPIs:\n{kpis}\n\n"
                    f"ROI report:\n{roi}\n\n"
                    'Produce a JSON object: {"suggestions": [OptimizationSuggestion, ...]}. '
                    "Max 3 suggestions. Sum of delta_pct * budget should approximate zero."
                    f"{schema_instruction(OptimizationSuggestion, array_key='suggestions')}"
                ),
            },
        ]

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        data = extract_json(content)
        raw_suggestions = data.get("suggestions", [])
        if not isinstance(raw_suggestions, list):
            raise ContractValidationError(
                agent_name=self.name,
                errors=[{"loc": ["suggestions"], "msg": "must be a list"}],
            )

        suggestions = []
        for s in raw_suggestions[:3]:
            try:
                suggestions.append(validate_payload(OptimizationSuggestion, s, self.name))
            except ContractValidationError:
                # Skip one malformed suggestion rather than failing the run.
                continue

        if raw_suggestions and not suggestions:
            raise ContractValidationError(
                agent_name=self.name,
                errors=[{"loc": ["suggestions"], "msg": "no valid suggestion objects"}],
            )

        return {"suggestions": suggestions}
