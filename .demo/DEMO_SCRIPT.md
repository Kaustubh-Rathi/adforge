# AdForge — Zero-Paid-API Demo Script

**Setup already done:** Docker (Postgres+Redis) running, seeded example campaigns,
API on :8000, dashboard on :8501, LLM = Coarena free tier (`gpt-6-luna`).

## 0. Before the audience arrives (dry run)

1. `make db-up && make seed` — 3 example campaigns with 30 days of mock KPIs (marked `is_mock=true`).
2. `make run-api` (terminal 1) and `make run-dashboard` (terminal 2).
3. Rehearse one full campaign run (below) so the traces + ROI pages are warm.

## 1. Opening — the pitch (1 min)

> "AdForge is an end-to-end multi-agent ad campaign platform. Give it a brief,
> seven specialized agents — research, creative, media planning, compliance,
> KPI analytics, ROI, and optimization — take it from idea to measured result.
> It runs on LangGraph with every agent owning its own MCP tool server, and
> this whole demo costs $0: free LLM via Coarena, free no-auth data APIs
> (DuckDuckGo, Wikipedia, Google Trends, SEC EDGAR, Jina Reader, Hacker News...),
> and mock ad-platform KPIs that are explicitly labelled as mock."

Open http://localhost:8501 — show the architecture diagram on the home page.

## 2. Show the seeded data (1 min)

- Sidebar → **Campaigns**: show the 3 seeded campaigns (tenant dropdown).
- Sidebar → **ROI Calculator**: pick `tenant_acme_saas` → "DTC SaaS — ProjectFlow launch".
  Show per-channel ROAS, CPA, net profit — the "was it worth it?" answer.
- Point out every KPI row is labelled `is_mock=true` (honest demo data).

## 3. Live run — create + run a campaign (3–5 min)

1. Sidebar → **Campaigns** → create new campaign (tenant `demo_tenant`):
   - Name: "Demo — Atlas Analytics B2B launch"
   - Product: "Privacy-first product analytics for B2B SaaS teams"
   - Budget $15,000, 14 days, channels google + linkedin, objective conversion.
2. Hit **Run**. Narrate as it goes:
   - **Research** — real live calls: Google Trends interest series, Wikipedia,
     keyword suggestions, competitor pages fetched through Jina Reader,
     Bing SERP fallback when DuckDuckGo rate-limits.
   - **Creative** — headline, primary text, A/B variants per channel.
   - **Media** — channel budget split, bidding, targeting, flight dates.
   - **Compliance** — PolicyEnforcer verdict; flagged issues + required disclosures.
3. The run **pauses for human review** — this is the HITL gate:
   - Sidebar → **Review Queue** → approve the campaign live (reviewer: `reviewer@adforge.local`).
   - After approval: **KPI analytics** pulls (mock, labelled) daily snapshots,
     **ROI calculator** computes ROAS/CPA/payback, **Optimization** proposes
     budget reallocation.
4. Sidebar → **Agent Traces** — click through each agent: input, output,
   token usage, audit events. "Every LLM call is logged; nothing is fabricated —
   missing credentials produce explicit errors, never fake data."
5. Sidebar → **ROI Calculator** — show the fresh campaign's verdict.

## 4. Closing (30 s)

> "The interesting part is the guardrails: multi-tenant isolation, per-tenant
> rate limiting, an append-only audit log, a compliance gate that hard-blocks
> prohibited content, and a cost ceiling per campaign. Swap in real ad-platform
> credentials and the same pipeline goes live against Google/Meta/LinkedIn/TikTok."

## Demo-day fallbacks

- **LLM quota hit (HTTP 429):** switch `LLM_MODEL=gemini-3-8-flash` in `.env`,
  restart the API, re-run. (`claude-*` ids are down until 2026-12-01.)
- **DuckDuckGo blocked:** the web_search chain falls back to Jina+Bing
  automatically (and Google Trends / Wikipedia still work keylessly).
- **Show something instantly:** the seeded campaigns + ROI page need no LLM at all.
