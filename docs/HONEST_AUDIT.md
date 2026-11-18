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

Plus the official LinkedIn Marketing API (`/rest/adAnalytics`, raw HTTP) and
the existing `tavily-python`. The unofficial `linkedin-api` package was removed:
it relies on undocumented endpoints and cannot be a production dependency.

These give the Research agent **real public data** for:
- "How much traffic does competitor X get?" → SimilarWeb
- "How is competitor Y's mobile app ranking + reviews?" → AppStore MCP
- "Is this audience IP range showing bot-like behavior?" → IPInfo + AbuseIPDB

The ad-platform APIs (Google Ads, Meta, LinkedIn Marketing, TikTok) still
require paid credentials — those are NOT public. When credentials aren't
available, the affected tool returns an explicit configuration error. AdForge
never fabricates ad performance, and a graph run records the failure instead
of pretending the channel succeeded.

---

## Q3. GitHub projects used

### v0.1 actually integrated (in pyproject.toml / docker-compose.yml)

| Repo | Purpose |
|------|---------|
| `langchain-ai/langchain-mcp-adapters` → migrated to `langchain[mcp]` | MCP↔LangGraph bridge via `MultiServerMCPClient` |
| `langchain-ai/langgraph` + `langgraph-checkpoint-postgres` + `langgraph-checkpoint-redis` | Graph orchestration + state persistence |
| `googleads/google-ads-mcp` | Official Google Ads read-only MCP (runs as its own docker service on :9006) |
| `modelcontextprotocol/python-sdk` (`mcp[cli]`) | Official MCP Python SDK — every local server uses `FastMCP` |
| `openai`, `anthropic` | LLM provider SDKs (default is the opencode.ai OpenAI-compatible endpoint) |
| `googleads/google-ads-python` | Optional ad-platform SDK extra; runtime KPI fetchers call public HTTP APIs directly |
| `facebookbusiness/facebook-python-business-sdk` | Optional self-hosted Meta SDK extra; not used by the default webhook publishing path |
| LinkedIn Marketing API | Official `/rest/adAnalytics` endpoint — wrapped as our own MCP (no official LinkedIn MCP exists) |
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
| `pipeboard-co/meta-ads-mcp` family | Requires Pipeboard OAuth — the remote MCP is registered only when `PIPEBOARD_API_TOKEN` is set; otherwise those tools are unavailable |
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
| KPI Analytics | Lists formulas | The node is deterministic and computes metrics from real platform reporting rows; the LLM does not recompute them. Cross-checking fetched numbers remains a roadmap item. |
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

1. **Live ad-platform writes use an adapter webhook.** The `publish_campaign`
   MCP calls an explicitly configured, authenticated publishing webhook after
   a single-use HITL token is consumed. Native Google Ads / Meta Marketing API
   write clients are not bundled yet; without the webhook, publishing fails
   explicitly and nothing is launched.
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

---

## v0.2.1 remediation and verification (2026-11-03)

A full defect-remediation pass followed this audit. The changes below are the
ones that most directly affect the concerns raised above.

| Area | Change |
|------|--------|
| LLM endpoint | Default provider is now `opencode` (`https://opencode.ai/inference/openai/v1`, `deepseek-v4.1-flash-free`). The client mirrors the reference behavior: Bun TLS fingerprint via `curl_cffi`, `<env>` system block, `ses_...` session headers, `read` tool gate, SSE streaming, and `stream_options.include_usage`. |
| Structured output | `adforge.agents.parsing.schema_instruction()` embeds the real Pydantic JSON Schema in every LLM agent prompt (research, creative, media planner, compliance, ROI, optimization). List outputs declare the wrapper shape. This substantially improves first-pass validation with small/free models and leaves the repair round for genuine mistakes. |
| Compliance targeting | `PolicyEnforcer._collect_targeting_terms()` now inspects only positive audience-targeting values. Exclusion/negative/`do_not_*` subtrees and descriptive fields (notes, policies, checks, summaries) are skipped, and the enforcer no longer stringifies the whole plan (which turned harmless notes into false `TARGETING_*` hard blocks). Regression tests cover exclusion lists, nested exclusions, `sensitive_attribute_check` self-assessments from a real live run, and real positive targeting. |
| Graph / API | HITL resume status derivation, failed-agent routing, trace queries, and tenant-scoped review updates are covered by unit + integration tests. |
| Storage / KPI | Engine init is idempotent, seeded KPI rows carry `source` + `platform_date`, analytics MCP coerces dates and returns JSON-safe rows, Redis failures fail open in test/dev. |
| Test tiers | The unit tier enforces a 90% coverage gate; the 90% gate for the full non-live suite covers unit + integration. Integration uses `embedded-postgres` 18 and `fakeredis`, so it does not require Docker. |

Verification on Windows, Python 3.14.6, project venv (2026-11-05):

| Check | Result |
|-------|--------|
| Unit tier (`not integration and not live`) | **584 passed**, **91.95%** coverage |
| Integration tier (`integration and not live`) | **42 passed** (behavioural subset; coverage is not meaningful in isolation) |
| Full non-live suite (`not live`) | **626 passed**, **95.32%** combined coverage |
| Docker runtime | Docker Desktop 4.98.0 + WSL2; engine 29.9.2, Compose v5.6.1; Postgres 16.15 and Redis 7 both healthy |
| App ↔ Docker services | SQLAlchemy/asyncpg reads, writes, and rollback against Postgres; Redis set/get/delete round-trip |
| Durable checkpointer | `AsyncPostgresSaver` selected against Docker Postgres; all four checkpoint tables created. A checkpoint written in one process was read back and deleted in a second process. The `localhost` → `127.0.0.1` normalisation avoids a psycopg IPv6-first stall (Docker publishes IPv4 loopback only). |
| Local MCP startup (`tests/test_mcp_startup_integration.py`) | 7/7 servers, **32 tools** discovered over real stdio handshakes |
| Live health (`ADFORGE_LIVE_TESTS=1`) | **1 passed, 4 skipped**; skipped channels have no ad-platform credentials |
| Live end-to-end (`tests/test_live_e2e.py`) | Passed 2026-11-05 (~331s): real opencode LLM → embedded Postgres → HITL pause → approve → `approved`, no token exposure. The post-checkpointer-change rerun skipped cleanly after 270s when the free-tier quota was exhausted mid-run; no spend occurred. The HITL review lifecycle and durable checkpointer are re-verified by the integration suite and the cross-process checkpoint test. |

Remaining environment limits (unchanged from the roadmap):

1. Real Google / Meta / LinkedIn / TikTok data requires credentials; live KPI
   tests skip without them. Offline request/response contracts are covered by
   `respx` fixtures, but real-account verification still needs credentials.
2. The unofficial `linkedin-api` package was removed (it cannot install on
   Python 3.14 and uses undocumented endpoints). The custom LinkedIn MCP uses
   the official `/rest/adAnalytics` endpoint over raw HTTP.
3. The opencode free tier is rate limited (`429 FreeUsageLimitError`). The live
   health test skips on quota exhaustion; the full live e2e consumes several
   calls and should be run deliberately.

## Zero-paid-key demo verification (2026-11-18)

A demo-hardening pass against a live stack (Docker Postgres 16 + Redis 7, API on
:8000, Streamlit on :8501, LLM = Coarena free tier). Everything below was
observed, not inferred from tests.

| Check | Result |
|-------|--------|
| Full non-live suite | **665 passed, 6 skipped** on the committed tree; **680 passed** after the dashboard/seed fixes in this pass. The skips are the live-service suites, which require `ADFORGE_LIVE_TESTS=1`. |
| End-to-end demo run (`.demo/verify_e2e.py`) | Create → run → HITL pause → approve → resume through KPI/ROI/optimization → traces/audit/KPI endpoints/isolation: **all checks pass**. Seven agent runs recorded, 55+ audit events, cross-tenant read returns 404. |
| Real research output | Live sources returned (Grand View Research / Future Market Insights product-analytics market reports); not fabricated. |
| Compliance behaviour | Correctly flags real copy against real channel limits (e.g. a headline over Google's 30-char cap) with `risk_level` and flagged issues. |

Defects found and fixed during this pass:

| Defect | Impact | Fix |
|--------|--------|-----|
| KPI API dropped `is_mock` | The dashboard's core honesty claim — "these figures are simulated" — could not be proven from the API at all. The column was populated, the response omitted it. | Snapshot and aggregate responses now return `is_mock`; aggregate also exposes `all_simulated` so a bucket mixing real and simulated rows is never reported as real. The ROI page renders a caption from that flag and tags each row Simulated/Measured. |
| `make seed` was not idempotent | Every run inserted fresh random campaign ids, cloning all three example campaigns and doubling their KPIs. In the live DB this had already produced two copies of each seeded campaign. | Campaign ids derive from tenant+name via `uuid5`; the insert upserts and the previous KPI rows are deleted first. Per-campaign RNG keeps the numbers reproducible. Verified by seeding twice: 4 campaigns / 119 KPI rows both times. |
| Theme overrode Streamlit's icon font | `[class*="st-"]` also matched Streamlit's icon spans, replacing Material Symbols — every icon rendered as raw ligature text (`keyboard_double_arrow_right`, `arrow_right`). | Body font scoped to text containers; icon font pinned explicitly. |
| Dashboard pinned to one tenant | The three seeded campaigns live in their own tenants, so the Campaigns page showed an empty console and the seeded tour in the demo script was impossible. | Sidebar tenant switcher backed by session state; pages read it through `api_client.current_tenant()`. Stale selections reset to the configured default. |

Still open for the demo (not defects, deliberate scope):

1. No video/live-recording artifact yet; `.demo/DEMO_SCRIPT.md` is the runbook.
2. `make lint` does not pass on `main`: `ruff check` reports 24 pre-existing
   findings (mostly FastAPI `Depends` in defaults, `B008`, plus test style
   rules), `ruff format --check` would reformat 94 files, and `mypy` reports 67
   errors across 22 files. None are introduced by this pass, but the lint gate
   advertised in the README does not currently hold.
3. Publishing still requires a real, authenticated webhook; `POST
   /reviews/{id}/publish` is the only path from `approved` to `live` and it
   stays disabled without `ADFORGE_ENABLE_LIVE_WRITES`.

## Dashboard UI pass (2026-11-19)

A second pass over the Streamlit console, driven by full-page screenshots at
1600x2000 rather than by the code alone. Every item below was reproduced in the
browser before it was changed.

| Defect | Impact | Fix |
|--------|--------|-----|
| Mermaid diagrams rendered into fixed-height iframes | The agent pipeline (a 2182x198 LR graph) was scaled to 50% inside a 520px box: 6px labels and 421px of dead space. The architecture graph overflowed its frame. | `render_mermaid` now strips the SVG's intrinsic size and sets `width/height: 100%` with `preserveAspectRatio="xMidYMid meet"`, so the whole diagram is centred at the largest size the frame allows. |
| Subgraph `direction LR` was ignored | Mermaid drops a subgraph's direction when its nodes have edges crossing the boundary. The pipeline collapsed into a 414x1158 vertical column and was unreadable at any frame height. | The pipeline now connects the `PLAN` and `MEASURE` subgraphs (not their nodes) and shows rejection as a labelled loop node. Native size is ~1251x556, which fills the frame at ~0.87 scale. |
| Primary button labels were invisible | The global `p { color: slate }` rule also matched the `<p>` inside Streamlit buttons, painting slate on pine. The Run button looked like an empty green rectangle. | Button descendants now inherit the button's colour; disabled buttons get an explicit muted style. |
| Main page appeared as "app" in the nav | Streamlit's legacy multipage nav uses the entrypoint filename as the page label. | `app.py` renamed to `Overview.py`; `Makefile` and `docker-compose.yml` updated. Ruff `N999` is ignored for that one file with a comment. |
| Four page-local `<style>` blocks duplicated the design system | Component CSS drifted between pages and was re-injected on every rerun; `.af-ops-row` used a 10px radius while the tokens said 6px. | All component CSS moved into `theme._CSS` (ledger rows, tables, dossiers, verdict banner). New `af_empty`, `af_note`, `af_table` and `af_verdict` helpers replace hand-built markup. A test fails if a page reintroduces `<style>`. |
| ROI verdict used a stale agent rationale as its explanation | The banner said "No KPI snapshots were provided" while the page displayed 14 days of seeded KPI rows. The rationale was written before the rows existed. | The banner now explains the verdict computed from the live buckets ("Google is the biggest drag at 0.51x"). The recorded agent rationale is demoted to a collapsed expander with a note that it may predate the data. |
| Empty and error states were browser-blue `st.info` blocks | The review queue showed a lone monospace `0` followed by a Streamlit info box; failures broke the palette. | `af_empty` panels for empty queues/campaigns/runs and `af_note` strips for warnings and failures. Traces gained a status filter; zero-cost runs are labelled as the free Coarena tier. |
| Streamlit chrome leaked into the demo | The "Deploy" button and toolbar appeared top-right on every page. | Toolbar/Deploy hidden; running indicator kept (agent runs take minutes). Sidebar carries an `AdForge / campaign operations console` wordmark above the nav. |

Verification:

| Check | Result |
|-------|--------|
| Full non-live suite | **691 passed, 6 skipped** (the skips are the live-service suites) |
| New theme tests | 11 tests pin escaping, numeric alignment, the viewport-fit script, the shared stylesheet, and the `Overview.py` entrypoint |
| Screenshots | Desktop 1600x2000 for all five pages; 760x1400 and 500x1400 narrow checks. No horizontal overflow (`scrollWidth == innerWidth`) at any width. |
| `ruff check` | Clean on every changed Python file. |

Known limitation: below 768px Streamlit renders the sidebar as an overlay and
lets the main column start at x=0, so an *open* sidebar covers the page. The
app now uses `initial_sidebar_state="auto"` so a phone starts with it collapsed;
a narrow desktop window between 641 and 767px can still show the overlay until
it is dismissed. Fixing that means overriding Streamlit's mobile layout, which
was judged riskier than the defect.
