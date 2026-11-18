# AdForge — Open-Source Dependency Audit

This document records what existing open-source projects AdForge integrates
rather than reinvents. The principle: **don't rewrite what the community has
already battle-tested**. Our job is the multi-agent orchestration layer and
the ad-campaign domain logic; everything below that line is delegated.

---

## TL;DR — Integration Decisions

| Layer | Decision | Source |
|-------|----------|--------|
| MCP ↔ LangGraph bridge | Use `langchain[mcp]` (the official post-migration package; `langchain-mcp-adapters` was archived Sep 2026 and merged in) | langchain-ai/langchain-mcp-adapters |
| Google Ads (read KPIs) | Depend on **googleads/google-ads-mcp** (official, read-only) via subprocess/pipx | github.com/googleads/google-ads-mcp |
| Google Ads (write — campaigns, bids) | No direct write client is bundled. Publishing goes through an explicitly configured, authenticated adapter webhook after HITL approval; writes are disabled by default. | new code |
| Meta + TikTok + Snap + Reddit Ads | Depend on **Pipeboard MCP family** (open-source, OAuth, free tier, 230+ tools). Self-host option available. | github.com/pipeboard-co/meta-ads-mcp |
| LinkedIn Ads (no official MCP) | Build our own small MCP server over the official `/rest/adAnalytics` API | new code, patterned after pipeboard-co |
| Multi-agent supervisor pattern | Reference **aws-samples/langgraph-multi-agent** (planning + execution split) | github.com/aws-samples/langgraph-multi-agent |
| Competitor research tools | Vendor ideas from CrewAI research patterns + a generic web-search MCP (Tavily/Brave) | various |
| LangGraph checkpointing | Use `langgraph-checkpoint-postgres` + `langgraph-checkpoint-redis` (official) | langchain-ai |
| LLM provider | opencode.ai OpenAI-compatible endpoint (default, free tier), with `openai` + `anthropic` as alternates | upstream SDKs |

---

## Detailed Repo Analysis

### 1. `googleads/google-ads-mcp` — OFFICIAL, USE DIRECTLY

- **Repo:** https://github.com/googleads/google-ads-mcp
- **Status:** Google official, open-sourced Oct 2025.
- **Capabilities:** Read-only. Tools: `search` (GAQL queries),
  `get_resource_metadata`, `list_accessible_customers`. Resources: discovery
  doc, metrics, segments, release notes.
- **Transport:** stdio (default), streamable-http (via FastMCP OAuth Proxy).
- **Auth:** OAuth 2.0 or Application Default Credentials. Developer token
  sunset Sep 9, 2026 — replaced by Google Cloud project access levels.
- **Install:** `pipx run --spec "git+https://github.com/googleads/google-ads-mcp.git" google-ads-mcp`
- **Why use it:** It's the canonical Google Ads MCP. Our KPI/Analytics agent
  calls it via `langchain[mcp]` `MultiServerMCPClient`.
- **Gap:** Read-only. Campaign create/update/budget changes are orchestrator-only
  and go through an authenticated adapter webhook after HITL approval; no native
  write client is bundled, and writes are disabled by default.

### 2. `pipeboard-co/meta-ads-mcp` — USE HOSTED REMOTE MCP

- **Repo:** https://github.com/pipeboard-co/meta-ads-mcp
- **Family:** Five remote MCP servers — Meta (42 tools), Google (59 tools),
  TikTok (59 tools), Snap (37 tools), Reddit (33 tools). 230+ tools total
  behind one OAuth.
- **Status:** Open-source. The hosted service (Pipeboard) is a badged Meta
  Business Partner with a free plan — no developer token required.
- **Auth:** OAuth at pipeboard.co, then a single `PIPEBOARD_API_TOKEN` works
  across all five servers.
- **Transport:** SSE (remote MCP URL e.g. `https://meta-ads.mcp.pipeboard.co/sse`).
- **Why use it:** Covers Meta + TikTok + (bonus) Snap + Reddit in one auth
  model. Frees us from vendoring four ad SDKs. Self-host option exists if a
  tenant needs it.
- **Gap:** Hosted service is metered (free tier exists but rate-limited). For
  fully-air-gapped deployments we'd self-host — same code, our own Meta app.

### 3. `langchain-ai/langchain-mcp-adapters` → migrated to `langchain[mcp]`

- **Repo:** https://github.com/langchain-ai/langchain-mcp-adapters (archived Sep 17, 2026)
- **Migration:** Code moved into `langchain.mcp` namespace. Install with
  `pip install "langchain[mcp]"`.
- **Key API:** `MultiServerMCPClient` — connects to N MCP servers (stdio /
  sse / streamable_http), exposes their tools as LangChain Tools, ready to
  bind to a LangGraph agent. Supports runtime headers (for auth tokens).
- **Why use it:** It's the canonical bridge. We construct a single
  `MultiServerMCPClient` at app startup with our 5+ MCP server configs.

### 4. `aws-samples/langgraph-multi-agent` — ARCHITECTURAL REFERENCE

- **Repo:** https://github.com/aws-samples/langgraph-multi-agent
- **Pattern:** Stateless LangGraph workflow with split planning vs execution
  agents. Graphs in `graphs/`, node implementations in `nodes/`, shared state
  in `state/`, entrypoint in `function/`.
- **Why reference it:** Validates our feature-folder layout (`agents/`,
  `graph/`, `state/`) and the supervisor + executor split — our
  Research/Creative/Media agents are planners, KPI/ROI/Optimization are
  executors. We adopt this folder convention verbatim.

### 5. `clientskatbi/google-ads-campaign-mcp` — WRITE-SIDE REFERENCE

- **Repo:** https://github.com/clientskatbi/google-ads-campaign-mcp
- **Capabilities:** Write-enabled Google Ads MCP — campaigns, budgets,
  keywords, search terms, keyword ideas.
- **Why reference it:** The official `google-ads-mcp` is read-only. For the
  Media Planner agent's "publish campaign" tool, we model our own write
  wrapper after this repo. We don't depend on it directly (small surface,
  easier to maintain a thin wrapper).

### 6. `crewai` research patterns — RESEARCH AGENT INSPIRATION

- **Patterns:** Competitor analysis crew, SERP tracking crew, SEO research
  crew — all documented in the CrewAI ecosystem.
- **Why reference it:** Our Research agent's tool roster (web search, SERP
  scrape, keyword clustering, creative extraction) maps directly to what
  these crews do. We re-implement the patterns as LangGraph tools backed by
  MCP servers, rather than depending on CrewAI (different orchestration
  framework — would create an awkward dependency).

### 7. LinkedIn Marketing API — CUSTOM MCP

- **Endpoint:** `https://api.linkedin.com/rest/adAnalytics` (official)
- **Why:** LinkedIn has no official MCP server (as of Oct 2026). Our small
  custom MCP in `adforge/mcp_servers/ads/linkedin.py` calls the documented
  reporting endpoint with a configured access token and ad-account ID. It
  fails explicitly when credentials are missing rather than returning
  fabricated analytics.

---

## What AdForge builds vs. integrates

### Vendored / depended on (not written by us)

1. `googleads/google-ads-mcp` — Google Ads KPI reads (official, pipx)
2. `pipeboard-co/meta-ads-mcp` family — Meta + TikTok + Snap + Reddit (remote OAuth)
3. `langchain[mcp]` — MCP → LangGraph bridge (official post-migration package)
4. `langgraph` + `langgraph-checkpoint-postgres` + `langgraph-checkpoint-redis`
5. `openai`, `anthropic` — alternate LLM providers (the default is the opencode.ai OpenAI-compatible endpoint)
6. `fastapi`, `streamlit`, `sqlalchemy`, `asyncpg`, `redis` — infra

### Built by us (the AdForge core)

1. **Guardrails layer** — contracts, tenant isolation, rate limits, audit log, policy
2. **7 agents** — Research, Creative, Media Planner, KPI Analytics, ROI, Compliance, Optimization
3. **7 first-party MCP servers** — research, ads, analytics, roi, compliance, public data, and LinkedIn Ads
4. **LangGraph orchestrator** — graph topology, state schema, checkpointer wiring
5. **FastAPI app** — campaign CRUD, agent invocation, KPI queries, trace endpoints
6. **Streamlit dashboard** — campaign overview, agent traces, ROI calculator
7. **3 example campaigns** — DTC SaaS, local restaurant, e-commerce
8. **Docker-compose orchestration** — Postgres + Redis + 7 MCP servers + API + dashboard

---

## Open questions / risks

- **Pipeboard free-tier rate limits:** Need to confirm quotas before
  shipping production campaigns. Self-host fallback exists.
- **LinkedIn API scope:** The official `/rest/adAnalytics` endpoint requires
  Marketing Developer Platform access and a configured ad-account ID. Live
  calls skip when credentials are absent rather than simulating data.
- **Google Ads write path:** The official MCP is read-only. Publishing is
  orchestrator-only and goes through an authenticated adapter webhook after a
  single-use HITL approval token; no write client is bundled, and writes are
  disabled unless explicitly enabled and configured.

---

## Conclusion

AdForge integrates several existing open-source repos and writes the seven
first-party MCP servers plus the multi-agent core on top. The custom work is
genuinely novel (multi-agent ad orchestration with guardrails + ROI calculator
+ compliance gate). The non-differentiated plumbing (ad-platform MCPs,
MCP-LangGraph bridge, checkpointing) is delegated to upstream projects.
