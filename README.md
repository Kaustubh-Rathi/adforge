# AdForge

**End-to-end multi-agent ad campaign platform.** Research → design → launch → measure → optimize → decide if it was worth it.

Built on LangGraph + Model Context Protocol (MCP), with each agent owning its persona + a dedicated MCP server. Designed to be **vendor-agnostic** and **horizontally distributable** — each MCP server runs in its own container, so no single instance carries the full load.

---

## What it does

AdForge equips companies to run their ads end-to-end. You give it a campaign brief, it:

1. **Researches** the market, competitors, keywords, and audience (Research Agent + Tavily MCP).
2. **Designs** ad creative concepts across channels (Creative Strategist Agent).
3. **Plans media** — channel mix, budget split, bidding (Media Planner Agent).
4. **Reviews compliance** — hard-blocks prohibited content, requires disclosures (Compliance Agent + PolicyEnforcer).
5. **Pulls live KPIs** from Google / Meta / LinkedIn / TikTok (KPI/Analytics Agent + googleads/google-ads-mcp + Pipeboard MCP).
6. **Calculates ROI** — ROAS, CPA, payback, recommendation (ROI Calculator Agent).
7. **Suggests optimizations** — reallocate budget based on live KPIs (Optimization Loop Agent).

The dashboard answers the only question that matters: **was it worth it?**

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                       Streamlit dashboard (8501)              │
│  Campaigns │ Agent Traces │ ROI Calculator                    │
└───────────────────────────────┬──────────────────────────────┘
                                │ HTTP
┌───────────────────────────────▼──────────────────────────────┐
│                       FastAPI (8000)                          │
│  /campaigns │ /kpis │ /traces                                  │
└───────────────────────────────┬──────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────┐
│           LangGraph orchestrator (7-agent graph)              │
│  research → creative → media → compliance → kpi →             │
│  roi → optimization                                           │
│  + Postgres checkpointer for resumable runs                   │
└──┬─────────┬──────────┬──────────┬──────────┬─────────────────┘
   │         │          │          │          │
   ▼         ▼          ▼          ▼          ▼
┌──────┐ ┌──────┐ ┌──────────┐ ┌──────┐ ┌─────────────┐
│resear│ │ ads  │ │analytics │ │ roi  │ │ compliance  │
│ MCP  │ │ MCP  │ │ MCP      │ │ MCP  │ │ MCP         │
│:9001 │ │:9002 │ │:9003     │ │:9004 │ │:9005        │
└──┬───┘ └──┬───┘ └──────────┘ └──────┘ └─────────────┘
   │        │
   │        ├─ googleads/google-ads-mcp (official, container :9006)
   │        ├─ Pipeboard remote MCP family (Meta + TikTok + Snap + Reddit)
   │        └─ adforge/mcp_servers/ads/linkedin.py (custom — no official MCP)
   │
   └─ Tavily web search API
```

See **[docs/DESIGN.md](docs/DESIGN.md)** for the full system design with 13 mermaid diagrams (architecture, agent graph, HITL sequence, MCP topology, permissions lifecycle, multi-tenant isolation, storage ERD, deployment topology, repo structure, GobbleCube plan summary) and **[docs/RESEARCH.md](docs/RESEARCH.md)** for the open-source dependency audit (what we integrate vs what we build).

---

## Tech stack

| Layer | Choice | Why |
|-------|--------|-----|
| Orchestration | LangGraph | Stateful multi-agent graphs; native Postgres checkpointer |
| Tools / MCP | Official `mcp[cli]` SDK + `langchain[mcp]` adapter | Industry standard, post-migration |
| LLM (default) | Z.ai GLM-4.6 | Cost-effective, fast; swap to OpenAI / Anthropic via env |
| Storage | Postgres (async SQLAlchemy) + Redis | Append-only audit log; LangGraph state cache |
| API | FastAPI + Uvicorn | Async, typed, OpenAPI docs out of the box |
| Dashboard | Streamlit + Plotly | Quick to iterate; enough for v1 |
| Ad platforms | googleads/google-ads-mcp + Pipeboard MCP family + custom LinkedIn wrapper | See docs/RESEARCH.md |

---

## Quick start

### Prerequisites

- Python 3.11+
- Docker + Docker Compose
- **Zero paid API keys required** — AdForge runs end-to-end with DuckDuckGo + Wikipedia + HN + Reddit + StackExchange + AppStore + IPInfo (all free, no auth). Set `ZAI_API_KEY` for LLM calls, or accept mock-mode for the LLM too.

### 1. Clone + install

```bash
git clone https://github.com/your-org/adforge.git
cd adforge
cp .env.example .env
# Edit .env — at minimum set ZAI_API_KEY (or accept mock mode)
make install
```

### 2. Boot Postgres + Redis

```bash
make db-up
make seed  # loads 3 example campaigns with KPI data
```

### 3. Run the API + dashboard

In one terminal:
```bash
make run-api    # http://localhost:8000
```

In another:
```bash
make run-dashboard  # http://localhost:8501
```

### 4. (Or) Boot the whole stack via docker-compose

```bash
make docker-up
make docker-logs  # tail logs
```

This brings up Postgres, Redis, all 5 local MCP servers, the official googleads/google-ads-mcp container, the FastAPI app, and the Streamlit dashboard — distributed across containers as designed.

---

## Usage

### Create a campaign

```bash
curl -X POST http://localhost:8000/campaigns \
  -H "X-AdForge-Tenant: acme" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Q1 SaaS launch",
    "brief": {
      "product_name": "Acme CRM",
      "product_description": "Lightweight CRM for B2B sales teams.",
      "target_audience": "Sales managers at 20-200 person B2B SaaS companies.",
      "budget_usd": 25000,
      "duration_days": 30,
      "business_objective": "conversion",
      "channels_hint": ["google", "linkedin"]
    }
  }'
```

### Run the agent graph

```bash
curl -X POST http://localhost:8000/campaigns/<campaign_id>/run \
  -H "X-AdForge-Tenant: acme" \
  -H "X-AdForge-Campaign: <campaign_id>"
```

This invokes all 7 agents in sequence: research → creative → media_planner → compliance → kpi_analytics → roi_calculator → optimization. Returns the final state with every agent's payload + usage stats.

### View results

Open `http://localhost:8501` and navigate to:
- **Campaigns** — list + create + run
- **Agent Traces** — inspect every agent's input, output, usage, errors, audit events
- **ROI Calculator** — per-channel ROAS, CPA, net profit, recommendation

---

## Guardrails

AdForge is opinionated about safety. Every agent invocation passes through:

1. **Typed contracts** (Pydantic) — input + output validated; broken agents can't corrupt downstream state.
2. **Tenant isolation** — `TenantScope` is injected into every DB query, MCP call, and cache key. One tenant's data never crosses another.
3. **Rate limiting** — per-tenant + per-campaign token buckets in Redis. One runaway campaign cannot starve others.
4. **Append-only audit log** — every LLM call, MCP tool call, and guardrail firing is persisted to `audit_events`. No UPDATE path exists.
5. **Compliance gate** — `PolicyEnforcer` hard-blocks prohibited terms (tobacco, weapons, financial claims without disclosure). The orchestrator will not invoke the publish tool without `approved=True`.
6. **Cost ceiling** — `guardrails_max_llm_calls_per_campaign` (default 200) bounds token spend per campaign.

These guardrails **cannot be disabled in prod or staging**. In dev/test you can set `APP_DISABLE_GUARDRAILS=true` to bypass rate limits for local iteration — the contracts + isolation still apply.

---

## Open-source dependencies

AdForge integrates rather than reinvents. See **[docs/RESEARCH.md](docs/RESEARCH.md)** for the full audit. Summary:

| Used as-is | For |
|-----------|-----|
| `googleads/google-ads-mcp` | Official Google Ads read-only MCP |
| `pipeboard-co/meta-ads-mcp` family | Meta + TikTok + Snap + Reddit (OAuth, free tier) |
| `langchain[mcp]` (post-migration) | MCP ↔ LangGraph bridge (`MultiServerMCPClient`) |
| `langgraph-checkpoint-postgres` + `langgraph-checkpoint-redis` | Resumable agent state |
| `aws-samples/langgraph-multi-agent` | Architectural reference (planner + executor split) |

| Built by us | Because |
|------------|---------|
| 5 local MCP servers | AdForge domain logic (research, ads, analytics, roi, compliance) |
| 7 agents | The novel multi-agent ad orchestration |
| Guardrails layer | No upstream package handles tenant isolation + audit + compliance |
| LinkedIn MCP wrapper | No official LinkedIn MCP exists (as of Oct 2026) |
| FastAPI + Streamlit app | Application surface |

---

## Configuration

All configuration is env-driven via `adforge/config.py`. See `.env.example` for the full list. Critical settings:

| Env var | Default | Purpose |
|---------|---------|---------|
| `APP_ENV` | `dev` | `dev`/`test`/`staging`/`prod` — affects guardrail strictness |
| `LLM_PROVIDER` | `glm` | `glm`/`openai`/`anthropic` |
| `ZAI_API_KEY` | (empty) | Z.ai GLM API key — required unless in mock mode |
| `DATABASE_URL` | auto-built from PG_* vars | Postgres connection string |
| `REDIS_URL` | auto-built | Redis connection string |
| `PIPEBOARD_API_TOKEN` | (empty) | If set, enables Meta + TikTok live data |
| `TAVILY_API_KEY` | (empty) | If set, enables real web search in Research agent |
| `GOOGLE_APPLICATION_CREDENTIALS` | (empty) | Path to GCP service-account JSON for google-ads-mcp |
| `GUARDRAILS_MAX_LLM_CALLS_PER_CAMPAIGN` | `200` | Hard ceiling on LLM spend per campaign |
| `GUARDRAILS_REQUIRE_COMPLIANCE_APPROVAL` | `true` | Block publish calls without Compliance verdict |

When API keys are absent, AdForge falls back to **mock mode** — every agent returns deterministic mock data so you can iterate on the dashboard + graph without spending money.

---

## Development

```bash
make lint            # ruff check + mypy
make test-unit       # pytest -m "not integration"
make test-integration  # requires Postgres + Redis
make test            # everything

# Run the agent eval suite (used by CI)
python -m adforge.utils.eval_runner --mock
```

### Add a new agent

See **[docs/ADDING_AN_AGENT.md](docs/ADDING_AN_AGENT.md)** for the step-by-step guide. Short version:

1. Create `adforge/agents/<name>/` with `__init__.py`, `persona.py`, `agent.py`.
2. Subclass `BaseAgent` — override `name`, `persona`, `build_messages`, `parse_response`.
3. Add a node to `adforge/graph/state.py`.
4. Wire it into the graph edges.
5. Add a test in `tests/test_agents.py`.

### Add a new MCP server

1. Create `adforge/mcp_servers/<name>.py` exposing a `mcp = FastMCP("adforge-<name>")` instance.
2. Register it in `adforge/mcp_servers/runner.py` `SERVERS` dict.
3. Add a service to `docker-compose.yml`.
4. Add a port + URL to `adforge/config.py`.
5. Register it in `adforge/mcp_servers/client.py::_build_client`.

---

## License

MIT. See [LICENSE](LICENSE).

---

## Status

AdForge is in **active development** (v0.1.x). The architecture + guardrails + agent contracts are stable; the agent LLM calls are wired but use mock data when no API keys are present. See the per-agent TODOs in `docs/ARCHITECTURE.md` for what's next.
