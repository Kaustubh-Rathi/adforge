"""AdForge guardrail exceptions.

All guardrail failures MUST raise one of these. The BaseAgent class catches
``GuardrailError`` and turns it into an ``AgentOutput`` with ``error`` set,
so a guardrail breach never crashes the graph — it produces a typed failure
that downstream nodes can route around.
"""

from __future__ import annotations


class GuardrailError(Exception):
    """Base class. Raise this (or a subclass) whenever a guardrail fires."""


class ContractValidationError(GuardrailError):
    """An agent's input or output failed Pydantic validation.

    Carries the agent name + the field path that failed so the trace UI can
    surface it.
    """

    def __init__(self, agent_name: str, errors: list[dict]) -> None:
        self.agent_name = agent_name
        self.errors = errors
        msg = f"[{agent_name}] contract validation failed: {errors}"
        super().__init__(msg)


class DataIsolationError(GuardrailError):
    """An agent tried to read or write data belonging to another tenant.

    This is the most serious guardrail failure — it must NEVER be swallowed.
    Production deployments alert on this.
    """

    def __init__(self, tenant_id: str, attempted_resource: str) -> None:
        self.tenant_id = tenant_id
        self.attempted_resource = attempted_resource
        msg = (
            f"[data-isolation] tenant={tenant_id!r} attempted access to "
            f"resource={attempted_resource!r} belonging to another tenant"
        )
        super().__init__(msg)


class RateLimitExceeded(GuardrailError):
    """An agent exceeded its LLM or ad-platform API call budget.

    Carries the limit + the window so the trace UI can show "you blew through
    60 calls/min on Google Ads API".
    """

    def __init__(self, scope: str, limit: int, window_seconds: int) -> None:
        self.scope = scope
        self.limit = limit
        self.window_seconds = window_seconds
        msg = (
            f"[rate-limit] scope={scope!r} exceeded {limit} calls "
            f"in {window_seconds}s window"
        )
        super().__init__(msg)


class PolicyViolation(GuardrailError):
    """An agent produced content that violated an advertising policy.

    The PolicyEnforcer raises this — typically for banned keywords,
    missing required disclosures, or platform-T&C violations.
    """

    def __init__(self, rule_id: str, detail: str) -> None:
        self.rule_id = rule_id
        self.detail = detail
        msg = f"[policy:{rule_id}] {detail}"
        super().__init__(msg)


class McpToolError(GuardrailError):
    """A tool call against an MCP server failed in a way the agent should know about.

    Distinct from a transport failure (which raises directly). This wraps
    ``CallToolResult(isError=True)`` results from MCP servers.
    """

    def __init__(self, server: str, tool: str, detail: str) -> None:
        self.server = server
        self.tool = tool
        self.detail = detail
        msg = f"[mcp:{server}/{tool}] {detail}"
        super().__init__(msg)
