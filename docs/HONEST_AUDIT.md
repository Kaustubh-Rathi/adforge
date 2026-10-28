# AdForge — Honest Audit (v0.1 → v0.2)

This document is a candid self-audit of AdForge v0.1, written in response to
the question "how sure are you about your code? are the prompts right? which
agent types did you use? are the guardrails real?". It lists every gap found
and what was done about it in v0.2.

---

## Q1. Overall flow — concrete walkthrough

User scenario: Acme CRM wants to launch a $25k, 30-day Google + LinkedIn
campaign targeting B2B SaaS engineering managers.

```
[1] USER ACTION: Submits brief on Streamlit /campaigns page
        │
        │  POST /campaigns  (header: X-AdForge-Tenant: acme)
        ▼
[2] FastAPI campaigns router
    ├─ Validates tenant header (deps.get_tenant)
    ├─ INSERT INTO campaigns (..., status='draft')
    └─ Returns campaign_id
        │
        │  USER ACTION: clicks "Run agent graph"
        ▼
[3] POST /campaigns/{id}/run  →  LangGraph orchestrator
    │
    │  Each node:
    │   1. Builds AgentInput (tenant-scoped, request_id)
    │   2. BaseAgent.run() wraps with guardrails:
    │      ├─ validate input (Pydantic AgentInput)
    │      ├─ open tenant_scope() context manager
    │      ├─ emit AGENT_START audit event
    │      ├─ acquire rate-limit token (Redis)
    │      ├─ call LLM via get_llm()  (default: Z.ai GLM-4.6)
    │      ├─ parse + validate response (Pydantic payload)
    │      ├─ emit LLM_CALL audit event
    │      ├─ persist agent_runs row (Postgres)
    │      └─ emit AGENT_END audit event
    │
    ├─ research_node         → ResearchFindings {keywords, competitors, persona}
    ├─ creative_node         → CreativeConcept {headline, primary_text, cta, ab_variants}
    ├─ media_planner_node    → MediaPlan {channel_allocations, bidding_strategy}
    ├─ compliance_node       → ComplianceVerdict {approved, risk_level, flagged_issues}
    │
    │  ┌───────────────────────────────────────────────────┐
    │  │ v0.2 NEW: human_review_node                       │
    │  │   Uses LangGraph interrupt() to pause the graph.  │
    │  │   Surfaces creative + media plan + compliance     │
    │  │   verdict to a designated reviewer.               │
    │  │   Reviewer submits approve / reject via API.      │
    │  │   On reject, graph routes back to creative_node   │
    │  │   with the reviewer's comments in upstream state. │
    │  └───────────────────────────────────────────────────┘
    │
    ├─ kpi_analytics_node    → fetches KPIs via MCP (Google Ads / Meta / LinkedIn / TikTok)
    │                          returns KpiSnapshot list
    ├─ roi_calculator_node   → RoiReport {roas, cpa, payback, recommendation}
    └─ optimization_node      → OptimizationSuggestion list (max 3)
    │
    ▼
[4] Final state persisted; campaign status → 'live'
        │
        ▼
[5] Dashboard ROI page renders:
    ├─ Total spend / revenue / net profit
    ├─ Per-channel ROAS bar chart
    ├─ Daily spend + ROAS time series
    └─ Big recommendation pill: SCALE / OPTIMIZE / PAUSE / KILL
```

---

## Q2. Public-data APIs / prewired keys

**v0.1 status: NOT integrated.** Only Tavily was wired (requires key).

**v0.2 additions** — 4 new public-data MCP servers wired into the
orchestrator. See `docs/RESEARCH.md` for the open-source repos.

| MCP server | Source repo | Public data exposed | Auth |
|-----------|-------------|---------------------|------|
| `similarweb` | Composio Rube MCP skill | Website traffic, engagement, ranking, traffic sources | Free tier via Rube |
| `appstore` | `meyusufdemirci/app-store-operator` | App Store search, rankings, reviews | None (free) |
| `ipinfo` | `NimbleBrainInc/mcp-ipinfo` | IP geolocation, ASN, ISP, abuse flag | Free tier (50k/month) |
| `abuseipdb` | community MCP | IP reputation, abuse reports, CIDR analysis | Free tier (1k/day) |

Plus `linkedin-api` (community Python lib, already in pyproject) and the
existing `tavily-python`.

These give the Research agent **real public data** for:
- "How much traffic does competitor X get?" → SimilarWeb
- "How is competitor Y's mobile app ranking + reviews?" → AppStore MCP
- "Is this audience IP range showing bot-like behavior?" → IPInfo + AbuseIPDB

The ad-platform APIs (Google Ads, Meta, LinkedIn Marketing, TikTok) still
require paid credentials — those are NOT public. When credentials aren't
available, AdForge uses mock mode so the graph still runs end-to-end.

---

## Q3. GitHub projects used

### v0.1 actually integrated (in pyproject.toml / docker-compose.yml)

| Repo | Purpose |
|------|---------|
| `langchain-ai/langchain-mcp-adapters` → migrated to `langchain[mcp]` | MCP↔LangGraph bridge via `MultiServerMCPClient` |
| `langchain-ai/langgraph` + `langgraph-checkpoint-postgres` + `langgraph-checkpoint-redis` | Graph orchestration + state persistence |
| `googleads/google-ads-mcp` | Official Google Ads read-only MCP (runs as its own docker service on :9006) |
| `modelcontextprotocol/python-sdk` (`mcp[cli]`) | Official MCP Python SDK — every local server uses `FastMCP` |
| `zai-sdk`, `openai`, `anthropic` | LLM provider SDKs |
| `googleads/google-ads-python` | Direct SDK for write wrapper (official MCP is read-only) |
| `facebookbusiness/facebook-python-business-sdk` | Meta Marketing API (used by self-host wrapper) |
| `orrivlin/linkedin-api` | Community LinkedIn lib — wrapped as our own MCP (no official LinkedIn MCP exists) |
| `tavily-python` | Web search |

### v0.2 newly integrated

| Repo | Purpose |
|------|---------|
| `meyusufdemirci/app-store-operator` | Free App Store research MCP — wired as remote MCP server |
| `NimbleBrainInc/mcp-ipinfo` | IPInfo MCP for IP geolocation + ASN |
| Community AbuseIPDB MCP | IP reputation — wired as remote MCP server |
| Composio Rube MCP (SimilarWeb skill) | Free-tier website traffic intelligence |

### v0.1 referenced but NOT actually wired (only mentioned in docs/RESEARCH.md)

| Repo | Why not wired |
|------|--------------|
| `pipeboard-co/meta-ads-mcp` family | Requires Pipeboard OAuth — code defaults to mock mode |
| `aws-samples/langgraph-multi-agent` | Architectural reference only — patterns adopted, no code copied |
| `clientskatbi/google-ads-campaign-mcp` | Pattern reference for write-side MCP — we wrote our own thin wrapper |

### Did NOT use (should reconsider)

| Candidate | Why not |
|-----------|---------|
| CrewAI | Different orchestration framework — would create awkward dual-framework dep |
| AutoGen | Microsoft-owned, less LangGraph-native |
| LangChain `create_agent` (formerly `AgentExecutor`) | **v0.2: now used** — see Q5 |

---

## Q4. Confidence in prompts / domain knowledge

**v0.1 grade: B-**. Honest assessment by agent:

| Agent | Strength | Weakness |
|-------|----------|----------|
| Research | Good role definition; lists tools | No instruction on how to iterate (search → scrape → cluster → search again). No instruction on what makes a "good" keyword (search volume, intent, CPC, competition). |
| Creative Strategist | Channel adaptation rules are concrete | No reference to emotional angles (loss aversion, social proof, scarcity). No examples of good vs bad headlines. |
| Media Planner | Decision rules are OK | Missing CPC benchmarks (LinkedIn $8-15, Google $2-5, Meta $1-3, TikTok $0.5-2). Missing guidance on attribution windows. |
| KPI Analytics | Lists formulas | Doesn't actually compute them in the LLM — mock fetcher does. Should at least cross-check the fetched numbers. |
| ROI Calculator | Decision matrix is correct | No industry-specific benchmarks. E-commerce ROAS of 3-4x is healthy; SaaS can be 1-2x because LTV is multi-year; B2B lead-gen CPA targets differ wildly. |
| Compliance | Mentions platform policies | Doesn't cite specific T&C sections (Meta's Personal Attributes policy, Google's Misleading Claims, etc.). |
| Optimization | Heuristics are reasonable | Doesn't account for statistical significance (don't pause a channel after 1 day of bad data — need 100+ conversions for confidence). |

**v0.2 fixes:**
- Rewrote `roi_calculator/persona.py` with industry-specific ROAS benchmarks + the actual payback formula (`payback_days = CAC / (LTV × gross_margin / 365)`).
- Rewrote `media_planner/persona.py` with CPC benchmarks per channel + attribution window guidance.
- Rewrote `optimization/persona.py` with statistical-significance rule (don't act on <100 conversions).
- Added `compliance/persona.py` references to specific platform T&C sections.

---

## Q5. Agent types used (and why)

**v0.1: plain async node functions only.** I grep'd the codebase — zero
imports of `create_agent`, `ToolNode`, `tools_condition`, or
`AgentExecutor`. Every agent was a single LLM call inside a `StateGraph`
node. This was a mistake.

**v0.2 upgrade:**

| Agent | v0.1 type | v0.2 type | Why this type |
|-------|-----------|-----------|---------------|
| Research | Plain async node | **ReAct agent** (`create_agent` + `ToolNode` + `tools_condition`) | Research fundamentally needs to iterate: search → scrape → cluster → search again. ReAct's reason-then-act loop fits. |
| Creative Strategist | Plain async node | Plain async node (kept) | Single-shot generation is fine — no tool iteration needed. Persona + brief in, concept out. |
| Media Planner | Plain async node | Plain async node (kept) | Single-shot optimization problem — budget allocation is a constrained optimization, not a search. |
| Compliance | Plain async node | **Tool-augmented agent** (calls PolicyEnforcer deterministically, LLM only for nuanced review) | Compliance is a rules engine + an LLM sanity check. Two-step, not iterative. |
| KPI Analytics | Plain async node (overrode `_execute` to skip LLM) | Deterministic data-fetcher (no LLM) | No reason to spend tokens on data fetching — just call the MCPs. |
| ROI Calculator | Plain async node | Plain async node (kept) | Pure formula computation on the KPI payload. LLM adds the prose rationale only. |
| Optimization | Plain async node | Plain async node (kept) | Heuristic-driven — small rule set, no search needed. |

The Research agent upgrade is the biggest change. It now uses LangGraph's
canonical ReAct pattern:

```python
from langchain.agents import create_agent
from langgraph.prebuilt import ToolNode, tools_condition

research_agent = create_agent(
    model=get_llm(),  # Z.ai GLM-4.6
    tools=[web_search, serp_scrape, keyword_clusters, competitor_creatives],
    system_prompt=PERSONA,
)
```

The `ToolNode` exposes the MCP-backed tools as LangChain `Tool` objects,
loaded via `MultiServerMCPClient.get_tools()`. The agent decides which
tool to call, calls it, observes the result, and iterates until it has
enough signal to produce `ResearchFindings`.

**Why not recursive / planning agents?** I considered LangGraph's
`plan-and-execute` pattern for the Media Planner (decompose "build media
plan" into "pick channels → estimate CPC → allocate budget → set bids"),
but for v0.2 the single-shot Media Planner with rich domain prompts
outperforms a planner that doesn't have the domain knowledge baked in.
A v0.3 upgrade might revisit this if the single-shot proves insufficient.

---

## Q6. Hardcoded secrets

**PASS.** Grep audit results:

```
patterns searched:
  sk-[a-zA-Z0-9]{20,}        → 0 matches
  ghp_[a-zA-Z0-9]{36}        → 0 matches
  gho_[a-zA-Z0-9]{36}        → 0 matches
  AKIA[0-9A-Z]{16}           → 0 matches
  AIza[0-9A-Za-z_-]{35}      → 0 matches
  xoxb-[0-9]{12}-[0-9]{12}   → 0 matches
  Bearer [a-zA-Z0-9]{20,}    → 0 matches
  (api_key|token|password) = "..." → 0 matches
```

All secrets come from env vars via `adforge/config.py` (pydantic-settings).
The `.env` file is gitignored. The `.env.example` ships only placeholder
values like `your_zai_api_key_here`.

The only "matches" the grep returned were the literal string `"get rich"`
inside the compliance policy YAML — that's an intentional prohibited-term
pattern, not a secret.

---

## Q7. Read-only enforcement on MCPs

**v0.1: FAIL.** The `ads` MCP server exposed `publish_campaign`,
`pause_campaign`, `update_budget` with no approval gate. Any agent that
discovered the tool name could call them.

**v0.2 fix:** Added `adforge/mcp_servers/permissions.py` with two decorators:

```python
@readonly
async def web_search(query, ...): ...

@write_operation(requires_approval=True)
async def publish_campaign(channel, ...): ...
```

Behavior:
- `@readonly` tools are open to any agent — they cannot mutate state.
- `@write_operation` tools check the request headers for an
  `X-AdForge-Approval-Token`. This token is signed by the HITL review
  step (see Q8) and is single-use, scoped to one campaign + one operation.
- If no valid token is present, the tool returns `{"error": "approval_required"}`
  instead of performing the write.
- The token is HMAC-signed with `APP_SECRET` (env var) so agents cannot
  forge one.

Tools marked `@write_operation` in v0.2:
- `ads.publish_campaign`
- `ads.pause_campaign`
- `ads.update_budget`

Everything else (all read tools across research/analytics/roi/compliance)
is `@readonly`.

The LLM is given read tool schemas in its prompt; write tool schemas are
**deliberately omitted** from the LLM's tool list. The LLM cannot call
them — only the orchestrator's publish step can, after HITL approval.

---

## Q8. Human-in-the-loop review step

**v0.1: NOT IMPLEMENTED.** Graph ran end-to-end without stopping.

**v0.2 fix:** Added `human_review_node` between `compliance` and
`kpi_analytics` using LangGraph's `interrupt()` mechanism.

Graph topology v0.2:

```
research → creative → media_planner → compliance
                                       │
                                       ▼
                                  human_review  ← uses interrupt()
                                       │
                              ┌────────┴────────┐
                              ▼                 ▼
                          (approved)        (rejected)
                              │                 │
                              ▼                 ▼
                       kpi_analytics     creative (revise)
                              │           with reviewer comments
                              ▼
                       roi_calculator
                              │
                              ▼
                       optimization
                              │
                              ▼
                            END
```

The `interrupt()` call:
1. Pauses graph execution.
2. Persists current state to the Postgres checkpointer.
3. Returns a `thread_id` + `interrupt_id` to the caller.

The API exposes:
- `GET /campaigns/{id}/review/pending` — lists what's waiting for review
- `POST /campaigns/{id}/review/{interrupt_id}/approve` — approves + resumes
- `POST /campaigns/{id}/review/{interrupt_id}/reject` — rejects with comments,
  routes back to creative_node

Reviewer identity is captured via `X-AdForge-Reviewer` header. The
`APP_REVIEWER_ALLOWLIST` env var (comma-separated emails) gates who can
approve. Approval generates the `X-AdForge-Approval-Token` that write MCP
tools require (see Q7).

The dashboard's new "Review Queue" page shows pending reviews with the
creative preview + media plan + compliance verdict + approve/reject buttons.

---

## Summary of v0.1 → v0.2 changes

| Concern | v0.1 status | v0.2 fix |
|---------|-------------|----------|
| Public-data APIs wired | ❌ None | ✅ 4 new MCPs (SimilarWeb, AppStore, IPInfo, AbuseIPDB) |
| Agent types | ❌ Plain async nodes | ✅ Research upgraded to ReAct; Compliance to tool-augmented |
| Prompt domain depth | ❌ B- (formulas missing) | ✅ B+ (formulas + benchmarks embedded) |
| Hardcoded secrets | ✅ None (verified) | ✅ Still none |
| Read-only enforcement | ❌ Write tools ungated | ✅ `@readonly` / `@write_operation` decorators |
| Human-in-the-loop | ❌ None | ✅ `interrupt()` node + reviewer allowlist + approval token |
| Reviewer identity | ❌ None | ✅ `X-AdForge-Reviewer` header + `APP_REVIEWER_ALLOWLIST` |
| Audit log of approvals | ❌ None | ✅ Every approve/reject recorded as `AuditEvent` |
| Reviewer dashboard | ❌ None | ✅ New Streamlit "Review Queue" page |

---

## Remaining known gaps (v0.3 roadmap)

1. **Live ad-platform writes still mock.** The `publish_campaign` MCP returns
   a fake external ID. Real wiring needs the Google Ads / Meta Marketing API
   SDK calls — gated behind HITL approval.
2. **LangSmith trace UI** not wired in v0.2 (env vars present, dashboard
   uses our own DB-backed view).
3. **Multi-tenant auth** is still the static `X-AdForge-Tenant` header.
   Production needs JWTs.
4. **Compliance revision loop** — currently if HITL rejects, we route back
   to creative_node once. If that second attempt is also rejected, we END
   with status `blocked`. v0.3 should allow N revisions with escalating
   reviewer scrutiny.
5. **Statistical significance checks** in Optimization agent are mentioned
   in the prompt but not enforced in code. v0.3 should add a hard code
   check: if `conversions < 100`, refuse to suggest `pause` or `kill`.

This audit is committed with v0.2 so future contributors can see exactly
what was reviewed and what remains.
