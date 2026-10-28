"""AdForge agent package — 7 agents + the BaseAgent class.

Each agent lives in its own feature folder:
    adforge/agents/research/         — Research agent + persona + tools
    adforge/agents/creative/         — Creative Strategist
    adforge/agents/media_planner/    — Media Planner
    adforge/agents/kpi_analytics/    — KPI / Analytics
    adforge/agents/roi_calculator/   — ROI Calculator
    adforge/agents/compliance/       — Compliance / QA
    adforge/agents/optimization/     — Optimization Loop

The BaseAgent class enforces guardrails (input/output validation, tenant
isolation, rate limiting, audit logging). Agents override ``build_messages``
and ``parse_response`` — never the guardrail logic.
"""

from adforge.agents.base import BaseAgent, AgentResult

__all__ = ["BaseAgent", "AgentResult"]
