# What AdForge is good for

AdForge is not a chat box that writes ad copy. It is a **pipeline with a
paper trail**: a brief goes in, seven agents work in a fixed order, a human
approves, and every step is written to an append-only audit log you can read
back. That shape decides what it is and is not good for.

---

## Use case 1 — B2B SaaS launch planning

**The situation.** A 30-person SaaS company is spending $25k over a quarter
across Google Search and LinkedIn. Whoever owns marketing is guessing at
keyword clusters, bidding strategy and budget split, and has no defensible
answer for the board when someone asks whether it worked.

**With AdForge.**

1. Brief in: product, audience, budget, duration, objective, channels.
2. **Research agent** pulls keyword clusters, competitor positioning and
   audience signals from free public sources (Wikipedia, DuckDuckGo/Bing,
   Google Trends, SEC EDGAR for competitor filings).
3. **Creative strategist** writes headlines and primary text per channel, with
   A/B variants, and is held to per-channel character limits.
4. **Media planner** splits the budget, picks bidding strategy, audience
   targeting and flight dates.
5. **Compliance agent** runs the copy through `PolicyEnforcer` — banned terms,
   missing disclosures, sensitive-attribute targeting. A 34-character Google
   headline against a 30-character cap is rejected here, not in production.
6. **You approve** — nothing is published before a named reviewer signs off.
7. **KPI analytics → ROI → optimization** measure, pronounce a verdict
   (`scale` / `optimize` / `pause` / `kill`) and propose the reallocation.

**What you get out.** A documented plan you can defend, plus a per-channel
ROAS/CPA table and a written rationale for the recommendation. The value is
mostly in step 5 and 7: the failure modes AdForge catches are the expensive
ones.

---

## Use case 2 — Pre-flight compliance review

**The situation.** A regulated vertical (finance, health, housing, employment)
runs everything past legal before it ships. Legal is a bottleneck, and the
review is repetitive.

**With AdForge.** Run the creative through the compliance node without
touching the rest of the pipeline. `PolicyEnforcer` is a tiered ruleset read
from YAML: hard blocks (tobacco, weapons), disclosure requirements (financial
claims need a disclaimer), soft warnings (superlatives). The verdict is a typed
`ComplianceVerdict` — `approved`, `risk_level`, `flagged_issues`,
`required_disclosures`, `blocked_terms` — stored in the audit log with the
exact rules that fired.

**Honest limit.** This is a first-pass filter, not a legal opinion. It catches
the mechanical violations; it cannot catch a claim that is technically legal
and substantively misleading. Keep the human reviewer.

---

## Use case 3 — An agency running many client accounts

**The situation.** An agency runs ten clients in one deployment. A bug or a
runaway loop on one account must not touch another.

**With AdForge.** Tenancy is structural, not a filter you remember to add:

- `TenantScope` is threaded through every DB query, MCP call and cache key.
- Every API request must carry `X-AdForge-Tenant`; there is no ambient tenant.
- Rate limits are per-tenant and per-campaign token buckets in Redis.
- `GUARDRAILS_MAX_LLM_CALLS_PER_CAMPAIGN` (default 200) caps spend per campaign.
- The audit log has no UPDATE path.

Tenant isolation is covered by integration tests that attempt cross-tenant
reads and assert they fail.

**Honest limit.** There is no per-tenant billing, quota dashboard or org
hierarchy. Tenant identity is a header, trusted from the caller — put AdForge
behind your own auth proxy before exposing it to clients.

---

## Use case 4 — Answering "was it worth it?"

**The situation.** A campaign finished. Spend and revenue are spread across
three ad platforms and nobody has a single number.

**With AdForge.** The ROI calculator recomputes ROAS, CPA, payback and net
profit from raw KPI rows — deliberately not from platform-reported aggregates,
so the arithmetic is auditable — and emits a verdict with a written rationale
naming the biggest drag and the best performer.

**Honest limit.** It computes what your KPI inputs say. If the ad platform
over-reports conversions, AdForge faithfully reports the inflated ROAS. It
does not validate platform attribution.

---

## Use case 5 — Teaching and evaluating multi-agent systems

**The situation.** You want a non-toy example of a LangGraph pipeline with real
guardrails, resumable human-in-the-loop state, MCP tool servers and a strict
typed contract at every agent boundary.

**With AdForge.** Every agent's input and output is a Pydantic model; the graph
state is a `TypedDict` with reducer annotations; the HITL gate is a real
`interrupt()` with a Postgres checkpointer, so a paused run survives a restart.
There are 691 tests across three tiers and an eval runner
(`python -m adforge.utils.eval_runner`) that scores agents against rubric checks.

**Honest limit.** AdForge is one opinionated implementation, not a benchmark.
The agent prompts are tuned for ad copy, not general reasoning.

---

## Where AdForge is the wrong tool

| You want | Why AdForge is wrong |
|---|---|
| **A chat interface** | There is none. You drive it through the dashboard or the REST API. A conversational front end is not built. |
| **Self-serve for non-technical users** | Setup needs Python, Docker and an `.env` file. It is an operator tool. |
| **Real spend today, out of the box** | Ad-platform credentials are not included. Mock KPIs (`is_mock=true`) fill the gap in dev; live spend needs Google/Meta/LinkedIn credentials. |
| **Creative generation at scale** | It produces a handful of concepts per run with a quality-over-volume bias. It is not a bulk variant engine. |
| **A media buyer replacement** | It proposes a plan and a reallocation. It does not negotiate, and it does not bid in real time. |
| **A CMS or an ad server** | It is a planning, review and measurement layer that sits above those. |

---

## Cost profile

The default path costs **$0** in API fees:

| Component | Default | Cost |
|---|---|---|
| LLM | Coarena free tier (`gpt-6-luna`) | $0 — $5/day cap, 30 req/min, 2 concurrent |
| Web search | DuckDuckGo → Bing RSS fallback | $0, no key |
| Market data | Wikipedia, Google Trends, SEC EDGAR, HN, Reddit, StackExchange, AppStore, IPInfo | $0, no key |
| Ad-platform KPIs | Mock, deterministic, marked `is_mock=true` | $0 |
| Postgres + Redis | Local Docker | $0 |
| **A full campaign run** | 7 agents, ~150k tokens | **$0**, 1–3 minutes |

Live ad-platform data needs whatever those platforms charge; AdForge adds no
markup. See [`RUNNING.md`](RUNNING.md) for the setup, and `.env.example` for
every credential that changes the answer.
