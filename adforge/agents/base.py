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

import json
import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from adforge.config import get_settings
from adforge.guardrails.audit_log import AuditEvent, AuditEventType, AuditLog, get_audit_log
from adforge.guardrails.contracts import AgentInput, AgentOutput
from adforge.guardrails.data_isolation import TenantScope, tenant_scope
from adforge.guardrails.exceptions import (
    ContractValidationError,
    GuardrailError,
    LLMQuotaExceededError,
    LLMTransportError,
)
from adforge.guardrails.rate_limiter import Scope, get_rate_limiter
from adforge.llm.base import LLMProvider, LLMResponse, LLMUsage
from adforge.llm.factory import get_llm

logger = logging.getLogger("adforge.agents")

_REPAIR_PROMPT = (
    "Your previous reply could not be parsed as a valid JSON object for the "
    "required schema. Return ONLY one JSON object, with no Markdown fences, "
    "no prose before it, and no prose after it."
)

#: Bounded retry for transient transport failures (429 / 5xx / socket errors).
_RETRY_ATTEMPTS = 3


def _is_retryable_llm_error(exc: BaseException) -> bool:
    """Retry transient transport errors, but never an exhausted quota."""
    return isinstance(exc, LLMTransportError) and not isinstance(exc, LLMQuotaExceededError)


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

    #: MCP servers whose read-only tools are exposed to this agent's LLM loop.
    #: Empty means the agent answers directly from the prompt.
    mcp_servers: tuple[str, ...] = ()

    def __init__(self, llm: LLMProvider | None = None) -> None:
        self._settings = get_settings()
        # Injectable for tests + the eval runner. Lazily resolved otherwise.
        self._llm: LLMProvider | None = llm
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
            input_failure = AgentOutput(
                request_id=raw_input.get("request_id") or UUID(int=0),
                tenant_id=raw_input.get("tenant_id", "unknown"),
                campaign_id=raw_input.get("campaign_id") or UUID(int=0),
                agent_name=self.name,
                payload={},
                error=f"input validation failed: {e}",
            )
            return AgentResult(input_failure)

        async with tenant_scope(agent_input.tenant_id, agent_input.campaign_id) as scope:
            await self._safe_audit(
                AuditEvent(
                    event_type=AuditEventType.AGENT_START,
                    tenant_id=scope.tenant_id,
                    campaign_id=scope.campaign_id,
                    agent_name=self.name,
                    request_id=agent_input.request_id,
                    payload={"input_summary": self._summarise(agent_input)},
                )
            )

            started_at = datetime.now(UTC)
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
                await self._safe_audit(
                    AuditEvent(
                        event_type=AuditEventType.GUARDRAIL_FIRE,
                        tenant_id=scope.tenant_id,
                        campaign_id=scope.campaign_id,
                        agent_name=self.name,
                        request_id=agent_input.request_id,
                        payload={"error": str(e), "type": type(e).__name__},
                    )
                )
            except Exception as e:
                # Unexpected error — log it loudly, surface to caller.
                logger.exception("[%s] unexpected failure", self.name)
                output = AgentOutput(
                    request_id=agent_input.request_id,
                    tenant_id=scope.tenant_id,
                    campaign_id=scope.campaign_id,
                    agent_name=self.name,
                    payload={},
                    error=f"unexpected error: {type(e).__name__}: {e}",
                )
                await self._safe_audit(
                    AuditEvent(
                        event_type=AuditEventType.AGENT_ERROR,
                        tenant_id=scope.tenant_id,
                        campaign_id=scope.campaign_id,
                        agent_name=self.name,
                        request_id=agent_input.request_id,
                        payload={"error": str(e), "type": type(e).__name__},
                    )
                )

            finished_at = datetime.now(UTC)
            await self._safe_audit(
                AuditEvent(
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
                )
            )

            # Persist the agent run (best-effort; non-fatal if DB is down).
            await self._persist_run(agent_input, output, started_at, finished_at)

            return AgentResult(output)

    # ------------------------------------------------------------------ #
    # Internal — the actual agent logic.
    # ------------------------------------------------------------------ #

    async def _execute(self, scope: TenantScope, agent_input: AgentInput) -> AgentOutput:
        """Build messages, run the read-only tool loop, parse, validate."""
        messages = self.build_messages(agent_input)

        # Rate limit check
        await get_rate_limiter(self.llm_scope).acquire(
            self.llm_scope,
            tenant_id=scope.tenant_id,
            campaign_id=str(scope.campaign_id),
        )

        llm = self._resolve_llm()
        tool_schemas, tool_index = await self._load_tools()

        if tool_schemas:
            content, usage = await self._run_tool_loop(
                llm, messages, tool_schemas, tool_index, scope, agent_input
            )
        else:
            response = await self._chat_audited(llm, messages, scope, agent_input)
            content = response.content
            usage = response.usage

        payload = await self._parse_with_repair(llm, messages, content, agent_input)

        return AgentOutput(
            request_id=agent_input.request_id,
            tenant_id=scope.tenant_id,
            campaign_id=scope.campaign_id,
            agent_name=self.name,
            payload=payload,
            usage=self._usage_dict(usage),
        )

    def _resolve_llm(self) -> LLMProvider:
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    # ------------------------------------------------------------------ #
    # Read-only MCP tool loop
    # ------------------------------------------------------------------ #

    async def _load_tools(self) -> tuple[list[dict[str, Any]], dict[str, tuple[Any, str]]]:
        """Load OpenAI-shaped schemas for this agent's read-only MCP tools.

        Write tools are filtered out by ``get_mcp_tools(..., read_only=True)``
        and are never advertised to the model. Any server failure is logged and
        skipped; the agent can still answer without tools.
        """
        if not self.mcp_servers:
            return [], {}

        from adforge.mcp_servers.client import get_mcp_tools

        schemas: list[dict[str, Any]] = []
        index: dict[str, tuple[Any, str]] = {}
        for server in self.mcp_servers:
            try:
                tools = await get_mcp_tools(server, read_only=True)  # type: ignore[arg-type]
            except Exception:
                logger.warning(
                    "[%s] could not load MCP tools from %r",
                    self.name,
                    server,
                    exc_info=True,
                )
                continue
            for tool in tools:
                name = str(getattr(tool, "name", "") or "")
                if not name or name in index:
                    continue
                index[name] = (tool, server)
                schemas.append(self._tool_schema(tool))
        return schemas, index

    @staticmethod
    def _tool_schema(tool: Any) -> dict[str, Any]:
        """Convert a LangChain/MCP tool into an OpenAI function schema."""
        schema = getattr(tool, "args_schema", None)
        if isinstance(schema, dict):
            parameters = dict(schema)
        elif schema is not None and hasattr(schema, "model_json_schema"):
            parameters = schema.model_json_schema()
        else:
            parameters = {"type": "object", "properties": {}}

        parameters.pop("title", None)
        properties = parameters.get("properties")
        if isinstance(properties, dict):
            # The orchestrator injects tenant headers; the model must not.
            properties.pop("headers", None)
        required = parameters.get("required")
        if isinstance(required, list) and "headers" in required:
            required.remove("headers")

        return {
            "type": "function",
            "function": {
                "name": str(getattr(tool, "name", "")),
                "description": str(getattr(tool, "description", "") or ""),
                "parameters": parameters,
            },
        }

    async def _run_tool_loop(
        self,
        llm: LLMProvider,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        tool_index: dict[str, tuple[Any, str]],
        scope: TenantScope,
        agent_input: AgentInput,
    ) -> tuple[str, LLMUsage]:
        """Call the model, execute requested read-only tools, repeat."""
        usage = LLMUsage()
        max_iterations = max(1, int(self._settings.agent_max_tool_iterations))

        for _ in range(max_iterations):
            response = await self._chat_audited(
                llm, messages, scope, agent_input, tools=tool_schemas
            )
            self._add_usage(usage, response.usage)

            if not response.tool_calls:
                if response.content.strip():
                    return response.content, usage
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Return the final JSON object now. Do not call any more tools."
                        ),
                    }
                )
                continue

            messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            for call in response.tool_calls:
                result = await self._invoke_tool(call, tool_index, scope, agent_input)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": result,
                    }
                )

        raise GuardrailError(
            f"{self.name} exceeded the read-only tool-call limit ({max_iterations} iterations)"
        )

    async def _invoke_tool(
        self,
        call: Any,
        tool_index: dict[str, tuple[Any, str]],
        scope: TenantScope,
        agent_input: AgentInput,
    ) -> str:
        """Execute one read-only tool with tenant headers injected."""
        entry = tool_index.get(call.name)
        if entry is None:
            result = json.dumps(
                {
                    "error": "tool_not_available",
                    "tool": call.name,
                    "detail": "Only read-only tools are exposed to agents.",
                }
            )
            await self._audit_tool_call(
                scope,
                agent_input,
                call.name,
                "unknown",
                call.arguments,
                ok=False,
                result_chars=len(result),
            )
            return result

        tool, server = entry
        ok = True
        try:
            raw = await tool.ainvoke(
                {
                    **call.arguments,
                    "headers": scope.as_headers(),
                }
            )
            result = self._tool_result_text(raw)
        except Exception as exc:  # noqa: BLE001 - report to the model
            ok = False
            result = json.dumps(
                {
                    "error": "tool_call_failed",
                    "tool": call.name,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )

        limit = max(512, int(self._settings.agent_max_tool_result_chars))
        if len(result) > limit:
            result = result[:limit] + "\n...[truncated]"

        await self._audit_tool_call(
            scope,
            agent_input,
            call.name,
            server,
            call.arguments,
            ok=ok,
            result_chars=len(result),
        )
        return result

    @staticmethod
    def _tool_result_text(raw: Any) -> str:
        """Normalise a LangChain/MCP tool result into text for the model."""
        if isinstance(raw, tuple) and len(raw) == 2:
            raw = raw[0]
        if hasattr(raw, "content"):
            raw = raw.content
        if isinstance(raw, list):
            parts: list[str] = []
            for block in raw:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif isinstance(block, str):
                    parts.append(block)
            if parts:
                raw = "\n".join(parts)
        if isinstance(raw, (dict, list)):
            return json.dumps(raw, default=str)
        return str(raw)

    async def _audit_tool_call(
        self,
        scope: TenantScope,
        agent_input: AgentInput,
        tool_name: str,
        server: str,
        arguments: dict[str, Any],
        *,
        ok: bool,
        result_chars: int,
    ) -> None:
        await self._safe_audit(
            AuditEvent(
                event_type=AuditEventType.MCP_TOOL_CALL,
                tenant_id=scope.tenant_id,
                campaign_id=scope.campaign_id,
                agent_name=self.name,
                request_id=agent_input.request_id,
                payload={
                    "tool": tool_name,
                    "server": server,
                    "argument_keys": sorted(arguments),
                    "ok": ok,
                    "result_chars": result_chars,
                },
            )
        )

    async def _chat_audited(
        self,
        llm: LLMProvider,
        messages: list[dict[str, Any]],
        scope: TenantScope,
        agent_input: AgentInput,
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Call the LLM with auditing + transient-error retry."""
        await self._safe_audit(
            AuditEvent(
                event_type=AuditEventType.LLM_CALL,
                tenant_id=scope.tenant_id,
                campaign_id=scope.campaign_id,
                agent_name=self.name,
                request_id=agent_input.request_id,
                payload={
                    "model": self._settings.llm_model or self._settings.llm_provider,
                    "message_count": len(messages),
                    "tool_count": len(tools or []),
                },
            )
        )

        response = await self._chat_with_retry(llm, messages, tools=tools)

        await self._safe_audit(
            AuditEvent(
                event_type=AuditEventType.LLM_RESPONSE,
                tenant_id=scope.tenant_id,
                campaign_id=scope.campaign_id,
                agent_name=self.name,
                request_id=agent_input.request_id,
                payload={
                    "finish_reason": response.finish_reason,
                    "content_length": len(response.content),
                    "tool_calls": [call.name for call in response.tool_calls],
                    "usage": {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    },
                },
            )
        )
        return response

    @staticmethod
    def _add_usage(total: LLMUsage, usage: LLMUsage) -> None:
        total.prompt_tokens += usage.prompt_tokens
        total.completion_tokens += usage.completion_tokens
        total.total_tokens += usage.total_tokens
        total.cost_usd += usage.cost_usd

    @staticmethod
    def _usage_dict(usage: LLMUsage) -> dict[str, Any]:
        return {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "cost_usd": usage.cost_usd,
        }

    async def _chat_with_retry(
        self,
        llm: LLMProvider,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Call the LLM, retrying only transient transport errors."""
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(_is_retryable_llm_error),
            stop=stop_after_attempt(_RETRY_ATTEMPTS),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            reraise=True,
        ):
            with attempt:
                return await llm.chat(messages, tools=tools)
        raise RuntimeError("unreachable")  # pragma: no cover

    async def _parse_with_repair(
        self,
        llm: LLMProvider,
        messages: list[dict[str, Any]],
        content: str,
        agent_input: AgentInput,
    ) -> dict[str, Any]:
        """Parse the LLM output; on failure make exactly one repair round."""
        first_error: ContractValidationError | None = None
        try:
            return self.parse_response(content, agent_input)
        except ContractValidationError as exc:
            first_error = exc
            logger.warning("[%s] first parse failed: %s", self.name, exc.errors)

        assert first_error is not None  # only reachable after a parse failure
        repair_messages = [
            *messages,
            {"role": "assistant", "content": content},
            {
                "role": "user",
                "content": (f"{_REPAIR_PROMPT}\n\nValidation errors: {first_error.errors}"),
            },
        ]
        repaired = await self._chat_with_retry(llm, repair_messages)
        try:
            return self.parse_response(repaired.content, agent_input)
        except ContractValidationError as second_error:
            raise ContractValidationError(
                agent_name=self.name, errors=second_error.errors
            ) from second_error

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
        domain schema. Subclasses must validate here and raise
        ``ContractValidationError`` on malformed output.
        """
        ...

    # ------------------------------------------------------------------ #
    # Persistence + audit
    # ------------------------------------------------------------------ #

    async def _safe_audit(self, event: AuditEvent) -> None:
        """Record an audit event without ever failing the agent run.

        Audit persistence depends on Postgres; a down database must not take
        the whole graph down. The failure is logged so it stays visible.
        """
        try:
            await self._audit.record(event)
        except Exception as exc:
            logger.warning(
                "[%s] audit event %s could not be persisted (%s: %s)",
                self.name,
                event.event_type.value,
                type(exc).__name__,
                exc,
            )

    async def _persist_run(
        self,
        agent_input: AgentInput,
        output: AgentOutput,
        started_at: datetime,
        finished_at: datetime,
    ) -> None:
        import json

        from sqlalchemy import text as sql_text

        from adforge.storage.session import get_session_factory

        try:
            factory = get_session_factory()
            async with factory() as session:
                await session.execute(
                    sql_text(
                        "INSERT INTO agent_runs "
                        "(tenant_id, campaign_id, request_id, agent_name, "
                        " input_payload, output_payload, usage, error, status, "
                        " started_at, finished_at) "
                        "VALUES (:tid, :cid, :rid, :an, CAST(:ip AS JSONB), "
                        "        CAST(:op AS JSONB), CAST(:usg AS JSONB), "
                        "        :err, :st, :sa, :fa)"
                    ),
                    {
                        "tid": output.tenant_id,
                        "cid": str(output.campaign_id),
                        "rid": str(output.request_id),
                        "an": output.agent_name,
                        "ip": json.dumps(agent_input.model_dump(mode="json"), default=str),
                        "op": json.dumps(output.payload, default=str),
                        "usg": json.dumps(output.usage, default=str),
                        "err": output.error,
                        "st": "succeeded" if output.error is None else "failed",
                        "sa": started_at,
                        "fa": finished_at,
                    },
                )
                await session.commit()
        except Exception as exc:
            # Persistence is best-effort; agent results still flow back to caller.
            # Log loudly (but concisely) so a broken trace pipeline is observable.
            logger.warning(
                "[%s] could not persist agent run to agent_runs (%s: %s)",
                self.name,
                type(exc).__name__,
                exc,
            )

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
