"""Media Planner agent node."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.media_planner.persona import PERSONA
from adforge.agents.parsing import extract_json, schema_instruction, validate_payload
from adforge.guardrails.contracts import AgentInput, MediaPlan
from adforge.guardrails.exceptions import ContractValidationError


class MediaPlannerAgent(BaseAgent):
    name = "media_planner"
    persona = PERSONA
    mcp_servers = ("ads", "roi")

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        brief = agent_input.brief.model_dump_json(indent=2) if agent_input.brief else "{}"
        research = json.dumps(agent_input.upstream_payloads.get("research", {}), indent=2, default=str)
        creative = json.dumps(agent_input.upstream_payloads.get("creative", {}), indent=2, default=str)

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        end = (datetime.now(UTC) + timedelta(days=agent_input.brief.duration_days if agent_input.brief else 30)).strftime("%Y-%m-%d")
        budget = agent_input.brief.budget_usd if agent_input.brief else None
        budget_rule = (
            f"HARD CONSTRAINT: total_budget_usd MUST be exactly {budget:.2f} "
            "(the brief's budget_usd). Never substitute a benchmark or example "
            "budget, and never exceed it."
            if budget is not None
            else "total_budget_usd MUST equal the brief's budget_usd."
        )

        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": (
                    f"Campaign brief:\n{brief}\n\n"
                    f"Research findings:\n{research}\n\n"
                    f"Creative concept:\n{creative}\n\n"
                    f"Suggested flight dates: {today} to {end}\n\n"
                    f"{budget_rule}\n\n"
                    "Produce a MediaPlan JSON object. Budget allocation must "
                    "sum to the brief's budget_usd. Stick to channels in the brief."
                    f"{schema_instruction(MediaPlan)}"
                ),
            },
        ]

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        data = extract_json(content)
        plan = validate_payload(MediaPlan, data, self.name)
        if (
            agent_input.brief is not None
            and plan["total_budget_usd"] > agent_input.brief.budget_usd + 0.01
        ):
            raise ContractValidationError(
                agent_name=self.name,
                errors=[{
                    "type": "budget_exceeds_brief",
                    "message": (
                        f"media plan total ${plan['total_budget_usd']:,.2f} "
                        f"exceeds the campaign budget "
                        f"${agent_input.brief.budget_usd:,.2f}"
                    ),
                }],
            )
        return plan
