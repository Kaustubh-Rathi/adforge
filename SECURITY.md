# Security policy

## Reporting a vulnerability

Do not open a public issue for a security problem. Use GitHub's private
reporting on this repository (Security → Report a vulnerability), or email the
maintainer listed on the GitHub profile.

Please include the affected version or commit, a reproduction, and what an
attacker gains. Expect an acknowledgement within 72 hours.

## Supported versions

`main` is the only supported branch. Pre-1.0 releases do not receive backported
fixes.

## Threat model — what AdForge does and does not protect against

AdForge holds credentials for a language model and, when configured, for ad
platforms. Treat it as an internal operator tool, not a public service.

**In scope, and implemented:**

- **Tenant isolation.** `TenantScope` is threaded through every DB query, MCP
  call and cache key. The API requires an explicit `X-AdForge-Tenant` header;
  there is no ambient or default tenant. Integration tests attempt cross-tenant
  reads and assert they fail.
- **Human approval before writes.** The graph pauses at a LangGraph
  `interrupt()`. Resuming requires an HMAC-signed, tenant-bound, single-use
  approval token minted from `APP_SECRET`; a replay is rejected. The publish
  tool is unreachable without it.
- **Reviewer authorisation.** `APP_REVIEWER_ALLOWLIST` gates who may approve.
  An empty allowlist is open **only** in dev/test; staging and prod refuse to
  start without one.
- **Append-only audit log.** Every LLM call, MCP tool call and guardrail firing
  is written to `audit_events`. There is no UPDATE path in the codebase.
- **Spend ceilings.** Per-tenant and per-campaign rate limits in Redis, plus a
  hard `GUARDRAILS_MAX_LLM_CALLS_PER_CAMPAIGN` ceiling.
- **Guardrail immutability.** Disabling guardrails is rejected when
  `APP_ENV` is `staging` or `prod`.

**Out of scope — you must provide:**

- **Caller authentication.** `X-AdForge-Tenant` is trusted from the caller.
  Anyone who can reach the API can claim any tenant. Put AdForge behind your
  own authenticating proxy, or set `API_AUTH_TOKEN` and send `X-API-Key`.
- **Network exposure.** Docker Compose publishes every port to `127.0.0.1`
  only. If you rebind to `0.0.0.0`, put a TLS-terminating proxy in front.
- **Secret storage.** `.env` is gitignored and must never be committed. In
  production, inject secrets from your platform's secret manager.
- **LLM prompt injection.** Agent inputs include text fetched from the public
  web. AdForge constrains every tool to read-only access and validates every
  agent's output against a Pydantic contract, which bounds the damage — but it
  does not claim to defeat prompt injection. A poisoned page can still skew
  research output.
- **Upstream ad-platform behaviour.** AdForge reports what the platform's
  reporting API returns. It does not validate attribution.

## Secrets in this repository

None. `.env` is gitignored, no credential is present in the working tree, and
secret scanning with push protection is enabled on the GitHub repository.
If you believe a credential was exposed, rotate it first, then report.
