# AdForge Architecture

This document covers the system design in depth. For a getting-started guide
see the [main README](../README.md). For the open-source dependency audit see
[RESEARCH.md](RESEARCH.md).

## Design principles

1. **Feature-based folders, not layered.** Each agent is a self-contained
   feature folder (`adforge/agents/research/`) with its persona, agent node,
   and tool wrappers. Adding an agent = adding a folder. Same for MCP servers.
2. **Guardrails are first-class.** The `guardrails/` package is imported
   before any agent code. Agents cannot bypass it because `BaseAgent.run()`
   wraps every call.
3. **Distributed by default.** Every MCP server runs in its own container.
   The API + dashboard + DB + Redis + MCP servers are all separate processes
   so we can scale horizontally per agent domain.
4. **Vendor-agnostic.** LLM provider + ad-platform MCPs are swappable via
   env vars. The default (GLM + mock ad data) runs end-to-end without any
   paid credentials.
5. **Audit everything.** Every LLM call, MCP tool call, guardrail firing
   is persisted to an append-only Postgres table. We can answer "why did
   your agent pause my campaign?" with evidence.

## Layer breakdown

### 1. Guardrails (`adforge/guardrails/`)

The non-negotiable safety layer. Imported first.

- **`contracts.py`** — Pydantic schemas for `AgentInput`, `AgentOutput`,
  and every domain payload (`CampaignBrief`, `ResearchFindings`,
  `CreativeConcept`, `MediaPlan`, `KpiSnapshot`, `RoiReport`,
  `ComplianceVerdict`, `OptimizationSuggestion`).
- **`exceptions.py`** — `GuardrailError` and subclasses
  (`ContractValidationError`, `DataIsolationError`, `RateLimitExceeded`,
  `PolicyViolation`, `McpToolError`). The `BaseAgent` catches these and
  converts them to `AgentOutput.error` so the graph keeps flowing.
- **`data_isolation.py`** — `TenantScope` dataclass + context manager.
  Every DB query, MCP call, and cache key is prefixed with the tenant.
  Production deployments add a SQLAlchemy event listener that rejects
  any SELECT without a `tenant_id` WHERE clause.
- **`rate_limiter.py`** — Redis-backed token bucket per scope (llm /
  google / meta / linkedin / tiktok) per tenant per campaign. Hard
  ceiling per campaign (`guardrails_max_llm_calls_per_campaign`).
- **`audit_log.py`** — Append-only writer to `audit_events` table.
  Uses raw SQL (no ORM model) so there's no UPDATE path.
- **`policy.py`** + `policy_rules.yaml` — `PolicyEnforcer` evaluates
  creative text + audience targeting against a 3-tier ruleset
  (hard-block / require-disclosure / soft-warning).

### 2. Storage (`adforge/storage/`)

- **`base.py`** — SQLAlchemy declarative base + `TenantMixin` + `TimestampMixin`.
- **`models.py`** — `Campaign`, `AgentRun`, `KpiSnapshotRow`,
  `CreativeArtifact`, `AuditEventRow` ORM models. All have `tenant_id`.
- **`session.py`** — async engine + session factory (singleton).
  Connection pool size 10 + 20 overflow.
- **`redis_client.py`** — async Redis singleton. Used by rate limiter +
  LangGraph checkpoint-redis (optional).
- **`alembic_env.py`** — async Alembic env. Migrations are auto-generated
  from model changes.
- **`seed.py`** — loads the 3 example campaigns + 7/14/21 days of KPI
  snapshots for dashboard testing.
- **`scripts/sql/init.sql`** — fallback DDL used by docker-compose
  entrypoint when Alembic isn't run.

### 3. LLM (`adforge/llm/`)

- **`base.py`** — abstract `LLMProvider` + normalised `LLMResponse` /
  `LLMUsage` types.
- **`zai_glm.py`** — default adapter. Calls Z.ai's OpenAI-compatible
  endpoint at `https://open.bigmodel.cn/api/paas/v4/chat/completions`.
- **`openai_provider.py`** — swap-in via `LLM_PROVIDER=openai`.
- **`anthropic_provider.py`** — swap-in via `LLM_PROVIDER=anthropic`.
- **`factory.py`** — `@lru_cache`d `get_llm()` that dispatches based on
  settings. Tests can clear the cache via `get_llm.cache_clear()`.

### 4. Agents (`adforge/agents/`)

Seven feature folders, one per agent. Each contains:

- `__init__.py` — exports the agent class
- `persona.py` — the system prompt (long, opinionated, defines the role)
- `agent.py` — subclasses `BaseAgent`, overrides `build_messages` +
  `parse_response`
- `tools.py` (only where the agent needs MCP tool wrappers — Research agent)

The `BaseAgent.run()` method enforces the guardrail flow:
1. Validate input via `AgentInput` (Pydantic).
2. Open `tenant_scope` context manager.
3. Emit `AGENT_START` audit event.
4. Call `_execute()` — subclass builds messages, calls LLM (rate-limited),
   parses response.
5. Emit `LLM_CALL` + `MCP_TOOL_CALL` audit events.
6. On `GuardrailError` — emit `GUARDRAIL_FIRE`, return `AgentOutput.error`.
7. On any other exception — emit `AGENT_ERROR`, return `AgentOutput.error`.
8. Emit `AGENT_END` audit event.
9. Persist `agent_runs` row.
10. Return `AgentResult`.

Agent ordering in the graph:

```
research → creative_strategist → media_planner → compliance →
  (if approved) → kpi_analytics → roi_calculator → optimization → END
  (if rejected) → creative_strategist (revision loop, v2)
```

For v1 the compliance rejection path is the same as approval — we still
proceed forward, but the `compliance_verdict` payload records the
rejection. v2 will route back to creative_strategist with a "revise"
hint. This is the only deliberate simplification in v1.

### 5. MCP servers (`adforge/mcp_servers/`)

Five local FastMCP servers, each in its own file:

| Server | Port | Tools | Backed by |
|--------|------|-------|-----------|
| `research.py` | 9001 | `web_search`, `serp_scrape`, `keyword_clusters`, `competitor_creatives` | Tavily API + mock fallback |
| `ads/__init__.py` | 9002 | `list_channels`, `publish_campaign`, `pause_campaign`, `update_budget` | Mock in v1; production delegates to googleads/google-ads-mcp + Pipeboard + LinkedIn MCP |
| `analytics.py` | 9003 | `persist_kpi_snapshot`, `query_kpis`, `aggregate_kpis` | Direct Postgres |
| `roi.py` | 9004 | `compute_roas`, `compute_cpa`, `compute_payback_period`, `classify_recommendation` | Pure Python formulas |
| `compliance.py` | 9005 | `evaluate_creative`, `list_blocked_terms`, `list_required_disclosures` | `guardrails.policy.PolicyEnforcer` |

Plus two **external** MCP servers we integrate (no local code):

| External | URL | Used for |
|----------|-----|----------|
| `googleads/google-ads-mcp` | `http://localhost:9006/mcp` | Official Google Ads read-only KPIs (GAQL queries) |
| Pipeboard MCP family | `https://*.mcp.pipeboard.co/sse` | Meta + TikTok + Snap + Reddit (OAuth via `PIPEBOARD_API_TOKEN`) |

The `runner.py` entrypoint boots any local MCP server by name:

```bash
python -m adforge.mcp_servers.runner research
python -m adforge.mcp_servers.runner ads
# ... etc.
```

Each runs as a separate docker-compose service — that's the load
distribution strategy the user asked for: no single instance carries
the full MCP load.

### 6. LangGraph orchestrator (`adforge/graph/`)

- **`state.py`** — `CampaignState` TypedDict, the graph topology
  (`build_graph()`), the compiled singleton (`get_compiled_graph()`),
  edge routing function (`route_after_compliance`).
- Postgres checkpointer wired (with graceful fallback to in-memory if
  DB is unavailable, e.g. in unit tests).
- Each agent has a thin async node wrapper that calls `agent.run()`,
  extracts the payload, and updates state.

The compiled graph is invoked by the `/campaigns/{id}/run` API endpoint.

### 7. API (`adforge/api/`)

- **`main.py`** — FastAPI app with lifespan-managed engine + Redis.
  CORS open in dev, tighten in prod.
- **`deps.py`** — `get_tenant`, `get_tenant_scope`, `get_db` FastAPI
  dependencies. Every route enforces the tenant header.
- **`routes/campaigns.py`** — create / list / get / run endpoints.
- **`routes/kpis.py`** — list snapshots + aggregate.
- **`routes/traces.py`** — list agent runs + audit events.

Every route requires `X-AdForge-Tenant` header. Tenant-scoped queries
filter by `tenant_id = :tid` so cross-tenant reads are impossible even
with a SQL injection.

### 8. Dashboard (`adforge/dashboard/`)

Streamlit multi-page app:

- **`app.py`** — landing page with architecture diagram.
- **`pages/1_Campaigns.py`** — list + create + run campaigns.
- **`pages/2_Agent_Traces.py`** — inspect runs, drill into input/output,
  view audit events.
- **`pages/3_ROI_Calculator.py`** — per-channel ROAS/CPA/CTR charts +
  recommendation verdict (SCALE/OPTIMIZE/PAUSE/KILL) +
  30-day time series.
- **`api_client.py`** — typed HTTP client to the FastAPI app.

## Data flow example

A user clicks "Run agent graph" on a campaign in the Streamlit dashboard:

1. **Streamlit** → POST `/campaigns/{id}/run` with `X-AdForge-Tenant` header.
2. **FastAPI** route handler:
   - Validates the campaign belongs to the tenant (DB read with tenant filter).
   - Updates campaign status to `designing`.
   - Builds initial `CampaignState`.
   - Calls `graph.ainvoke(state, config={"configurable": {"thread_id": ...}})`.
3. **LangGraph** invokes each node in turn:
   - `research_node` → `ResearchAgent.run()` → LLM call → return `ResearchFindings`.
   - `creative_strategist_node` → upstream `research` payload in `AgentInput.upstream_payloads`.
   - ... and so on through all 7 nodes.
4. Each `BaseAgent.run()`:
   - Validates input via `AgentInput`.
   - Opens `tenant_scope`.
   - Acquires rate-limit token.
   - Calls LLM (via `get_llm()`).
   - Parses response via subclass's `parse_response`.
   - Validates output schema.
   - Persists `agent_runs` row.
   - Emits audit events.
5. Graph completes → FastAPI returns final state.
6. **Streamlit** displays the final state as JSON.
7. User navigates to the ROI Calculator page → `GET /kpis/{id}/aggregate` → chart rendered.

## Deployment

### Local dev

```bash
make install
make db-up
make seed
make run-api        # terminal 1
make run-dashboard  # terminal 2
```

### Docker

```bash
make docker-up
# Brings up:
#   postgres:16-alpine
#   redis:7-alpine
#   adforge-mcp-research       (port 9001)
#   adforge-mcp-ads            (port 9002)
#   adforge-mcp-analytics      (port 9003)
#   adforge-mcp-roi            (port 9004)
#   adforge-mcp-compliance     (port 9005)
#   adforge-mcp-google-ads-official (port 9006)
#   adforge-api                (port 8000)
#   adforge-dashboard          (port 8501)
```

### Production

- Each MCP server can be scaled horizontally by running N replicas behind
  a load balancer. They're stateless (state lives in Postgres + Redis).
- The FastAPI app is also stateless — scale behind your LB of choice.
- The Streamlit dashboard is single-instance (it's a dev tool); for
  production UI you'd swap to a Next.js app talking to the same API.
- Postgres: use a managed instance (RDS / Cloud SQL). Single writer.
- Redis: managed (ElastiCache / Memorystore). Used for rate limits +
  LangGraph checkpointing.

## What's NOT in v1

Deliberately deferred:

1. **MCP tool-calling loop inside agents.** The Research agent has MCP
   tools wired but doesn't yet drive a multi-turn tool-call loop with
   the LLM. v1 relies on the LLM's own knowledge for research; v2 will
   add the `experimental_tool_loop` flag.
2. **Live ad-platform write operations.** `publish_campaign` MCP tool
   returns a mock external ID. Production needs to call Google Ads API
   (via `google-ads-python` since the official MCP is read-only),
   Meta Marketing API, etc.
3. **Compliance revision loop.** When the Compliance agent rejects, we
   currently proceed forward (with the rejection recorded). v2 will
   route back to `creative_strategist` with a "revise per these issues"
   hint.
4. **LangSmith / LangGraph Studio trace UI.** Settings are wired
   (`LANGSMITH_TRACING`) but the dashboard's "Agent Traces" page uses
   our own DB-backed view for now. LangSmith integration is a one-line
   config change away.
5. **Real LinkedIn Marketing API integration.** The wrapper structure
   exists; live calls are TODO. Mock mode is the default.

## Open questions

- **Pipeboard rate limits** on the free tier — need to validate before
  shipping production campaigns. Self-host fallback exists.
- **Google Ads write path** — official MCP is read-only. We'll write
  our own thin wrapper over `google-ads-python` for the Media Planner's
  `publish_campaign` tool. Small surface area.
- **Multi-tenant auth on the API** — v1 uses a static `X-AdForge-Tenant`
  header. v2 should issue per-tenant JWTs.
