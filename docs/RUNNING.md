# Running AdForge

Every command here was executed on a real Windows 11 + Docker Desktop machine
before being written down. Paths assume the repo is at `D:\AD`; substitute
yours.

`make` is **not** required. The `Makefile` is kept for Linux/CI, where `make`
and a POSIX shell exist. On Windows use the PowerShell blocks below — the
Makefile recipes use `sleep` and `$(NAME)`, which do not work under
`cmd.exe`/PowerShell without a POSIX shell.

---

## 0. One-time setup

```powershell
cd D:\AD

# Python 3.11+ (3.14 also works) and Docker Desktop must already be installed.
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install the app + dev tools + dashboard extras.
pip install -e ".[dev,dashboard]"

cp .env.example .env
```

Then edit `.env` and set at minimum:

| Variable | Why |
|---|---|
| `APP_ENV=dev` | Enables seed data + mock ad-platform KPIs |
| `APP_SECRET` | HMAC key for HITL approval tokens. **Required** outside dev |
| `APP_REVIEWER_ALLOWLIST` | Comma-separated reviewer emails allowed to approve |
| `LLM_API_KEY` | Coarena free key (`$5/day`). Without it, agent LLM calls fail explicitly |
| `LLM_MODEL` | `gpt-6-luna`; fall back to `gemini-3-8-flash` if you hit HTTP 429 |

No paid ad-platform credentials are needed. Their absence produces explicit
configuration errors — AdForge never fabricates a live platform response.

---

## 1. Start infrastructure

Postgres holds campaigns, KPI rows and the append-only audit log. Redis holds
the rate-limit buckets and LangGraph's checkpoint cache.

```powershell
docker compose up -d postgres redis
docker compose ps          # both should report (healthy)
```

Expected:

```
adforge-postgres   Up (healthy)   127.0.0.1:5432->5432/tcp
adforge-redis      Up (healthy)   127.0.0.1:6379->6379/tcp
```

Load the three example campaigns (idempotent — re-running rewrites the same
rows instead of duplicating them):

```powershell
.\.venv\Scripts\python.exe -m adforge.storage.seed
```

You get one campaign per tenant, each with 30 days of `is_mock=true` KPI rows:
`tenant_acme_saas`, `tenant_bloom_bistro`, `tenant_vertex_audio`.

---

## 2. Start the API

Terminal 1:

```powershell
cd D:\AD
.\.venv\Scripts\python.exe -m uvicorn adforge.api.main:app `
  --host 127.0.0.1 --port 8000 --reload
```

Verify:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
# status version
# ------ -------
# ok     0.1.0
```

Interactive API reference: <http://127.0.0.1:8000/docs>

---

## 3. Start the dashboard

Terminal 2:

```powershell
cd D:\AD
.\.venv\Scripts\python.exe -m streamlit run adforge/dashboard/Overview.py `
  --server.port=8501
```

Open <http://localhost:8501>. Five pages appear in the sidebar:

| Page | What it answers |
|---|---|
| **Overview** | Architecture, the 7-agent pipeline, live operations board |
| **Campaigns** | Create a brief, run the graph, watch spend |
| **Agent Traces** | Every agent's input, output, usage and audit events |
| **ROI Calculator** | Was it worth it? Per-channel ROAS, CPA, net profit |
| **Review Queue** | Approve or reject a paused campaign (the HITL gate) |

Use the sidebar **Tenant** switcher to move between the seeded tenants and
`demo_tenant` (where live runs land).

---

## 4. Run a campaign end to end

### Through the UI

1. **Campaigns** → *Create new campaign*. Fill name, product, audience, budget,
   duration, objective, channels.
2. **Run agent graph**. Seven agents execute in sequence: research → creative →
   media planner → compliance → **pause** → kpi analytics → ROI → optimization.
3. The run stops at the human-in-the-loop gate. **Review Queue** → read the
   packet (creative + media plan + compliance verdict) → approve as
   `reviewer@adforge.local`.
4. **Agent Traces** and **ROI Calculator** now show the completed campaign.

The full run takes 1–3 minutes on the free LLM tier.

### Through the API

```powershell
$h = @{ "X-AdForge-Tenant" = "demo_tenant"; "Content-Type" = "application/json" }

$body = @{
  name  = "Q1 SaaS launch"
  brief = @{
    product_name        = "Acme CRM"
    product_description = "Lightweight CRM for B2B sales teams."
    target_audience     = "Sales managers at 20-200 person B2B SaaS companies."
    budget_usd          = 25000
    duration_days       = 30
    business_objective  = "conversion"
    channels_hint       = @("google", "linkedin")
  }
} | ConvertTo-Json -Depth 5

$c = Invoke-RestMethod -Method Post http://127.0.0.1:8000/campaigns `
       -Headers $h -Body $body
$c.id

# Run the graph (pauses at the HITL gate).
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/campaigns/$($c.id)/run" `
  -Headers @{ "X-AdForge-Tenant" = "demo_tenant";
              "X-AdForge-Campaign" = $c.id }
```

---

## 5. Verify the machine before demoing

Two scripts exercise the whole pipeline against the live API. Both use
throwaway tenants and clean up after themselves.

```powershell
.\.venv\Scripts\python.exe .demo\verify_e2e.py
.\.venv\Scripts\python.exe .demo\verify_review_paths.py
```

`verify_e2e.py` covers create → run → approve → KPI/ROI/optimization → traces.
`verify_review_paths.py` covers the two paths the first script skips: reject
(routes back to the creative agent) and publish (refuses without a configured
adapter). Each prints a `PASS`/`FAIL` line per check and exits non-zero on
failure.

---

## 6. Run the whole stack in Docker

Use this when you want one command instead of three terminals. It builds the
API, the dashboard and all seven MCP servers as separate containers.

```powershell
docker compose up -d --build
docker compose logs -f --tail=100       # tail
docker compose down                     # stop
docker compose down -v                  # stop + drop the Postgres volume
```

Ports are published to `127.0.0.1` only: `8000` (API), `8501` (dashboard),
`9001`–`9007` and `9012` (MCP servers).

---

## 7. Stop / restart the local processes

```powershell
# Kill whatever is holding the API and dashboard ports.
Get-NetTCPConnection -LocalPort 8000,8501 -State Listen |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

---

## 8. Development commands

```powershell
.\.venv\Scripts\ruff.exe check adforge tests        # lint
.\.venv\Scripts\ruff.exe format --check adforge tests
.\.venv\Scripts\mypy.exe adforge                    # strict types

.\.venv\Scripts\python.exe -m pytest -m "not integration and not live"  # unit
.\.venv\Scripts\python.exe -m pytest -m "integration and not live"      # embedded Postgres
.\.venv\Scripts\python.exe -m pytest                                    # everything
```

Live tests are opt-in and call the configured LLM + ad-platform endpoints.
They skip automatically when credentials or free-tier quota are unavailable.

```powershell
$env:ADFORGE_LIVE_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -m live --timeout=900
```

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Dashboard shows "API offline" and dashes | API not running, or wrong `DASHBOARD_API_URL` | Start terminal 1; it defaults to `http://localhost:8000` |
| `X-AdForge-Tenant header is required` | Header missing | Every API call needs the tenant header |
| HTTP 429 from the LLM | Coarena free-tier quota | Set `LLM_MODEL=gemini-3-8-flash` in `.env`, restart the API |
| `Refusing to seed sample campaigns outside dev/test` | `APP_ENV` is not `dev`/`test` | Seeded rows are fabricated; they are refused in staging/prod by design |
| Review Queue empty after a run | The run did not reach the HITL gate | Check **Agent Traces** for a compliance block or agent error |
| `PoolClosed` / checkpointer errors | Postgres not up or `CHECKPOINTER_BACKEND` wrong | `docker compose ps`; keep `CHECKPOINTER_BACKEND=postgres` |
| Approve returns 403 | Reviewer not in `APP_REVIEWER_ALLOWLIST` | Add the email, restart the API (empty allowlist = dev-open) |
| Streamlit page blank | Stale websocket | Hard-refresh; or restart the dashboard process |
