"""AdForge guardrails package.

Every agent's input and output MUST flow through this package. It enforces:
    * Typed contracts (Pydantic schemas) for every agent's input/output.
    * Tenant / data isolation — one tenant's campaign never crosses another.
    * Rate limits on LLM + ad-platform API calls.
    * Append-only audit log of every agent decision.
    * Compliance policy enforcement (banned keywords, required disclosures).

This module is the single source of truth for "is this agent allowed to do X?".
The BaseAgent class delegates to these guardrails — agents cannot bypass them.
"""

from adforge.guardrails.contracts import (
    AgentInput,
    AgentOutput,
    CampaignBrief,
    CreativeConcept,
    MediaPlan,
    KpiSnapshot,
    RoiReport,
    ComplianceVerdict,
    OptimizationSuggestion,
    ResearchFindings,
)
from adforge.guardrails.exceptions import (
    GuardrailError,
    ContractValidationError,
    DataIsolationError,
    RateLimitExceeded,
    PolicyViolation,
)
from adforge.guardrails.data_isolation import tenant_scope, assert_tenant_access
from adforge.guardrails.rate_limiter import RateLimiter
from adforge.guardrails.audit_log import AuditLog
from adforge.guardrails.policy import PolicyEnforcer

__all__ = [
    "AgentInput",
    "AgentOutput",
    "CampaignBrief",
    "CreativeConcept",
    "MediaPlan",
    "KpiSnapshot",
    "RoiReport",
    "ComplianceVerdict",
    "OptimizationSuggestion",
    "ResearchFindings",
    "GuardrailError",
    "ContractValidationError",
    "DataIsolationError",
    "RateLimitExceeded",
    "PolicyViolation",
    "tenant_scope",
    "assert_tenant_access",
    "RateLimiter",
    "AuditLog",
    "PolicyEnforcer",
]
