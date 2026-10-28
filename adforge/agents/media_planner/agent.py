"""Media Planner agent node."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from adforge.agents.base import BaseAgent
from adforge.agents.media_planner.persona import PERSONA
from adforge.guardrails.contracts import AgentInput, MediaPlan


class MediaPlannerAgent(BaseAgent):
    name = "media_planner"
    persona = PERSONA

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        brief = agent_input.brief.model_dump_json(indent=2) if agent_input.brief else "{}"
        research = json.dumps(agent_input.upstream_payloads.get("research", {}), indent=2, default=str)
        creative = json.dumps(agent_input.upstream_payloads.get("creative", {}), indent=2, default=str)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        end = (datetime.now(timezone.utc) + timedelta(days=agent_input.brief.duration_days if agent_input.brief else 30)).strftime("%Y-%m-%d")

        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": (
                    f"Campaign brief:\n{brief}\n\n"
                    f"Research findings:\n{research}\n\n"
                    f"Creative concept:\n{creative}\n\n"
                    f"Suggested flight dates: {today} to {end}\n\n"
                    "Produce a MediaPlan JSON object. Budget allocation must "
                    "sum to the brief's budget_usd. Stick to channels in the brief."
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
                "channel_allocations": [{"channel": "google", "budget_usd": 1000.0}],
                "total_budget_usd": 1000.0,
                "bidding_strategy": "max_conversions",
                "audience_targeting": {},
                "expected_reach": 0,
                "expected_impressions": 0,
                "flight_dates": {"start": "2026-01-01", "end": "2026-02-01"},
                "_parse_error": str(e),
            }
        plan = MediaPlan(**data)
        return plan.model_dump()
