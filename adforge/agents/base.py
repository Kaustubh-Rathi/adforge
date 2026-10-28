"""BaseAgent — every agent in the graph inherits from this.

Responsibilities:
    * Wrap input in AgentInput (validated).
    * Set up TenantScope for data isolation.
    * Acquire LLM rate limit tokens before each chat call.
    * Record AuditEvents for every LLM + MCP tool call.
    * Parse + validate output into AgentOutput (typed payload).
    * Persist the agent run to the ``agent_runs`` table.
    * On any GuardrailError, return AgentOutput.error instead of crashing.

Agents override:
    name            — 'research', 'creative_strategist', etc.
    persona         — system prompt defining the agent's character + skills
    build_messages  — turn AgentInput -> list[chat message]
    parse_response  — turn LLM content -> typed payload dict

Agents must NEVER:
    * Bypass tenant scope
    * Call ad-platform MCPs without rate-limit check
    * Persist anything outside the guardrails.audit_log
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from adforge.config import get_settings
from adforge.guardrails.contracts import AgentInput, AgentOutput
from adforge.guardrails.data_isolation import TenantScope, tenant_scope
from adforge.guardrails.exceptions import GuardrailError
from adforge.guardrails.rate_limiter import get_rate_limiter, Scope
from adforge.guardrails.audit_log import AuditLog, AuditEvent, AuditEventType, get_audit_log
from adforge.llm.factory import get_llm
from adforge.llm.base import LLMUsage


class AgentResult:
    """Result of an agent run. Wraps AgentOutput + the LLM usage stats."""

    def __init__(self, output: AgentOutput) -> None:
        self.output = output

    @property
    def ok(self) -> bool:
        return self.output.error is None


class BaseAgent(ABC):
    """Abstract base class for all AdForge agents."""

    #: Agent identifier — must match the name in graph node registry.
    name: str = "base"

    #: Default system prompt — overridden by subclasses.
    persona: str = "You are an AdForge agent."

    #: Rate-limit scope for this agent's LLM calls. Defaults to "llm".
    llm_scope: Scope = "llm"

    def __init__(self) -> None:
        self._settings = get_settings()
        self._llm = None
        self._audit: AuditLog = get_audit_log()

    # ------------------------------------------------------------------ #
    # Public entry point — called by the LangGraph node.
    # ------------------------------------------------------------------ #

    async def run(self, raw_input: dict[str, Any]) -> AgentResult:
        """Top-level entry. Validates input, runs agent, validates output.

        Never raises GuardrailError — converts it to AgentOutput.error so the
        graph can route around it.
        """
        try:
            agent_input = AgentInput(**raw_input)
        except Exception as e:
            output = AgentOutput(
                request_id=raw_input.get("request_id") or UUID(int=0),
                tenant_id=raw_input.get("tenant_id", "unknown"),
                campaign_id=raw_input.get("campaign_id") or UUID(int=0),
                agent_name=self.name,
                payload={},
                error=f"input validation failed: {e}",
            )
            return AgentResult(output)

        async with tenant_scope(agent_input.tenant_id, agent_input.campaign_id) as scope:
            await self._audit.record(AuditEvent(
                event_type=AuditEventType.AGENT_START,
                tenant_id=scope.tenant_id,
                campaign_id=scope.campaign_id,
                agent_name=self.name,
                request_id=agent_input.request_id,
                payload={"input_summary": self._summarise(agent_input)},
            ))

            started_at = datetime.now(timezone.utc)
            output: AgentOutput
            try:
                output = await self._execute(scope, agent_input)
            except GuardrailError as e:
                output = AgentOutput(
                    request_id=agent_input.request_id,
                    tenant_id=scope.tenant_id,
                    campaign_id=scope.campaign_id,
                    agent_name=self.name,
                    payload={},
                    error=str(e),
                )
                await self._audit.record(AuditEvent(
                    event_type=AuditEventType.GUARDRAIL_FIRE,
                    tenant_id=scope.tenant_id,
                    campaign_id=scope.campaign_id,
                    agent_name=self.name,
                    request_id=agent_input.request_id,
                    payload={"error": str(e), "type": type(e).__name__},
                ))
            except Exception as e:
                # Unexpected error — log it loudly, surface to caller.
                output = AgentOutput(
                    request_id=agent_input.request_id,
                    tenant_id=scope.tenant_id,
                    campaign_id=scope.campaign_id,
                    agent_name=self.name,
                    payload={},
                    error=f"unexpected error: {type(e).__name__}: {e}",
                )
                await self._audit.record(AuditEvent(
                    event_type=AuditEventType.AGENT_ERROR,
                    tenant_id=scope.tenant_id,
                    campaign_id=scope.campaign_id,
                    agent_name=self.name,
                    request_id=agent_input.request_id,
                    payload={"error": str(e), "type": type(e).__name__},
                ))

            finished_at = datetime.now(timezone.utc)
            await self._audit.record(AuditEvent(
                event_type=AuditEventType.AGENT_END,
                tenant_id=scope.tenant_id,
                campaign_id=scope.campaign_id,
                agent_name=self.name,
                request_id=agent_input.request_id,
                payload={
                    "duration_seconds": (finished_at - started_at).total_seconds(),
                    "ok": output.error is None,
                    "usage": output.usage,
                },
            ))

            # Persist the agent run (best-effort; non-fatal if DB is down).
            await self._persist_run(agent_input, output, started_at, finished_at)

            return AgentResult(output)

    # ------------------------------------------------------------------ #
    # Internal — the actual agent logic.
    # ------------------------------------------------------------------ #

    async def _execute(self, scope: TenantScope, agent_input: AgentInput) -> AgentOutput:
        """Build messages, call LLM (rate-limited), parse, validate."""
        messages = self.build_messages(agent_input)

        # Rate limit check
        await get_rate_limiter(self.llm_scope).acquire(
            self.llm_scope,
            tenant_id=scope.tenant_id,
            campaign_id=str(scope.campaign_id),
        )

        if self._llm is None:
            self._llm = get_llm()

        await self._audit.record(AuditEvent(
            event_type=AuditEventType.LLM_CALL,
            tenant_id=scope.tenant_id,
            campaign_id=scope.campaign_id,
            agent_name=self.name,
            request_id=agent_input.request_id,
            payload={
                "model": self._settings.llm_provider,
                "message_count": len(messages),
            },
        ))

        response = await self._llm.chat(messages)

        payload = self.parse_response(response.content, agent_input)

        return AgentOutput(
            request_id=agent_input.request_id,
            tenant_id=scope.tenant_id,
            campaign_id=scope.campaign_id,
            agent_name=self.name,
            payload=payload,
            usage={
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "cost_usd": response.usage.cost_usd,
            },
        )

    # ------------------------------------------------------------------ #
    # Subclass hooks
    # ------------------------------------------------------------------ #

    @abstractmethod
    def build_messages(self, agent_input: AgentInput) -> list[dict[str, str]]:
        """Convert AgentInput to a list of chat messages for the LLM."""
        ...

    @abstractmethod
    def parse_response(self, content: str, agent_input: AgentInput) -> dict[str, Any]:
        """Parse LLM content into a dict payload that conforms to the agent's
        domain schema. Subclasses are responsible for validation here —
        the BaseAgent trusts whatever dict they return.
        """
        ...

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    async def _persist_run(
        self,
        agent_input: AgentInput,
        output: AgentOutput,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        from adforge.storage.session import get_session_factory
        from sqlalchemy import text as sql_text
        try:
            factory = get_session_factory()
            async with factory() as session:
                await session.execute(
                    sql_text(
                        "INSERT INTO agent_runs "
                        "(tenant_id, campaign_id, request_id, agent_name, "
                        " input_payload, output_payload, usage, error, status, "
                        " started_at, finished_at) "
                        "VALUES (:tid, :cid, :rid, :an, :ip::jsonb, :op::jsonb, "
                        "        :usg::jsonb, :err, :st, :sa, :fa)"
                    ),
                    {
                        "tid": output.tenant_id,
                        "cid": str(output.campaign_id),
                        "rid": str(output.request_id),
                        "an": output.agent_name,
                        "ip": __import__("json").dumps(agent_input.model_dump(mode="json"), default=str),
                        "op": __import__("json").dumps(output.payload, default=str),
                        "usg": __import__("json").dumps(output.usage, default=str),
                        "err": output.error,
                        "st": "succeeded" if output.error is None else "failed",
                        "sa": started_at,
                        "fa": finished_at,
                    },
                )
                await session.commit()
        except Exception as e:
            # Persistence is best-effort; agent results still flow back to caller.
            # In production we'd ship this to a dead-letter queue.
            pass

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _summarise(self, agent_input: AgentInput) -> dict[str, Any]:
        """Small, log-safe summary of the input — never includes full brief."""
        return {
            "agent": self.name,
            "brief_present": agent_input.brief is not None,
            "upstream_payload_keys": list(agent_input.upstream_payloads.keys()),
        }
