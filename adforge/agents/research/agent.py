"""Research Agent — LangGraph node + LLM message builder."""
from __future__ import annotations

import json
from typing import Any

from adforge.agents.base import BaseAgent, AgentResult
from adforge.agents.research.persona import PERSONA
from adforge.guardrails.contracts import AgentInput, ResearchFindings


class ResearchAgent(BaseAgent):
    name = "research"
    persona = PERSONA

    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        brief = agent_input.brief
        brief_text = brief.model_dump_json(indent=2) if brief else "(no brief provided)"

        upstream = json.dumps(agent_input.upstream_payloads, default=str)

        return [
            {"role": "system", "content": self.persona},
            {
                "role": "user",
                "content": (
                    f"Campaign brief:\n{brief_text}\n\n"
                    f"Upstream agent outputs (may be empty):\n{upstream}\n\n"
                    "Produce a ResearchFindings JSON object. Use the MCP tools "
                    "available to you (web_search, serp_scrape, keyword_clusters, "
                    "competitor_creatives) before finalising your answer. "
                    "Respond with ONLY the JSON object — no prose."
                ),
            },
        ]

    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        # Strip markdown code fences if the model wrapped its answer.
        body = content.strip()
        if body.startswith("```"):
            body = body.split("\n", 1)[1]
            if body.endswith("```"):
                body = body.rsplit("```", 1)[0]

        try:
            data = json.loads(body)
        except json.JSONDecodeError as e:
            # Best-effort fallback — let the LLM retry next pass.
            data = {
                "market_summary": body[:500],
                "top_keywords": [],
                "sources": [],
                "_parse_error": str(e),
            }

        # Validate against schema — raises if missing required fields.
        findings = ResearchFindings(**data)
        return findings.model_dump()

    async def run(self, raw_input: dict[str, Any]) -> AgentResult:
        # For v1, we skip the MCP tool-calling loop and rely on the LLM's
        # own knowledge to produce findings. The tools are wired but the
        # full agent loop (call LLM, parse tool calls, invoke tools, loop)
        # is a TODO controlled by settings.experimental_tool_loop flag.
        return await super().run(raw_input)
