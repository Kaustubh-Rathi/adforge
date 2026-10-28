# AdForge — System Design (Mermaid Diagrams)

This is the single source of truth for AdForge's architecture. Every diagram here is rendered as a Mermaid block — view this file in GitHub, GitLab, VS Code, or any markdown viewer that supports Mermaid.

## Table of contents

1. [High-level architecture](#1-high-level-architecture)
2. [Agent graph topology (with HITL)](#2-agent-graph-topology-with-hitl)
3. [End-to-end sequence (campaign run)](#3-end-to-end-sequence-campaign-run)
4. [HITL review sequence (approve / reject)](#4-hitl-review-sequence-approve--reject)
5. [MCP server topology + permissions](#5-mcp-server-topology--permissions)
6. [Permission / approval-token lifecycle](#6-permission--approval-token-lifecycle)
7. [Multi-tenant data isolation](#7-multi-tenant-data-isolation)
8. [LLM provider abstraction](#8-llm-provider-abstraction)
9. [Web-search provider chain (Tavily-free)](#9-web-search-provider-chain-tavily-free)
10. [Storage layer + checkpointing](#10-storage-layer--checkpointing)
11. [Docker-compose deployment topology](#11-docker-compose-deployment-topology)
12. [Feature-based repo structure](#12-feature-based-repo-structure)
13. [GobbleCube plan summary](#13-gobblecube-plan-summary)

---

## 1. High-level architecture

```mermaid
flowchart TB
    subgraph UI["User-facing layer"]
        Streamlit["Streamlit dashboard :8501<br/>Campaigns / Traces / ROI / Review Queue"]
    end

    subgraph API["API layer (FastAPI :8000)"]
        CampRoutes["/campaigns<br/>create / list / run"]
        KpiRoutes["/kpis<br/>snapshots / aggregate"]
        TraceRoutes["/traces<br/>runs / audit events"]
        ReviewRoutes["/reviews<br/>pending / approve / reject"]
    end

    subgraph Graph["Orchestration layer (LangGraph)"]
        Orchestrator["Compiled StateGraph<br/>+ Postgres checkpointer"]
    end

    subgraph Agents["7 agent feature folders"]
        Research["ResearchAgent<br/>(ReAct)"]
        Creative["CreativeStrategist"]
        Media["MediaPlanner"]
        Comply["Compliance"]
        Hitl["human_review<br/>(interrupt)"]
        Kpi["KpiAnalytics"]
        Roi["RoiCalculator"]
        Opt["Optimization"]
    end

    subgraph Guard["Guardrails"]
        Contracts["Pydantic contracts"]
        Tenant["TenantScope"]
        RateLimit["Redis rate limiter"]
        Audit["Append-only audit log"]
        Policy["PolicyEnforcer"]
    end

    subgraph MCP["MCP server layer (8 servers)"]
        ResearchMCP["research :9001<br/>read-only"]
        AdsMCP["ads :9002<br/>read + write-gated"]
        AnalyticsMCP["analytics :9003<br/>read-only"]
        RoiMCP["roi :9004<br/>read-only"]
        ComplyMCP["compliance :9005<br/>read-only"]
        PublicDataMCP["public_data :9007<br/>read-only<br/>(10+ free APIs)"]
        GoogleOfficial["google-ads-mcp :9006<br/>(official, read-only)"]
        Pipeboard["Pipeboard remote MCPs<br/>(Meta + TikTok + Snap + Reddit)"]
    end

    subgraph Storage["Storage"]
        Postgres[("Postgres 16<br/>campaigns / agent_runs /<br/>kpi_snapshots / audit_events")]
        Redis[("Redis 7<br/>rate limits + state cache")]
    end

    Streamlit -->|HTTP| API
    API --> Orchestrator
    Orchestrator --> Research & Creative & Media & Comply & Hitl & Kpi & Roi & Opt
    Agents --> Guard
    Agents --> MCP
    Guard --> Storage
    MCP --> Storage
    GoogleOfficial -.read.-> Postgres
    Pipeboard -.OAuth.-> ExternalAdAPIs["Google / Meta / TikTok /<br/>Snap / Reddit ad APIs"]
```

### What each layer is responsible for

| Layer | Responsibility | Cannot do |
|-------|---------------|-----------|
| UI (Streamlit) | Render campaign list, ROI charts, agent traces, review queue | Bypass API — every call goes through FastAPI |
| API (FastAPI) | Validate tenant header, route to graph, persist runs | Call MCPs directly (only orchestrator does) |
| Orchestration (LangGraph) | Decide node order, persist state, route on conditions | Skip guardrails — every node runs through BaseAgent |
| Agents | Domain logic — what to ask LLM, how to parse response | Bypass TenantScope or call write tools without approval |
| Guardrails | Contracts, isolation, rate limit, audit, policy | Make business decisions (only enforce rules) |
| MCPs | Wrap external APIs, enforce read-only / write-gated permissions | Make decisions — pure tool execution |
| Storage | Persist state, append-only audit log | Update audit_events (no UPDATE path exists) |

---

## 2. Agent graph topology (with HITL)

```mermaid
flowchart LR
    START((START)) --> Research[research_node<br/>ReAct agent<br/>iterates: search → scrape → cluster]
    Research --> Creative[creative_strategist_node<br/>produces CreativeConcept]
    Creative --> Media[media_planner_node<br/>produces MediaPlan]
    Media --> Comply[compliance_node<br/>runs PolicyEnforcer + LLM review]
    Comply --> Hitl{human_review_node<br/>uses interrupt()}
    Hitl -->|approved| Kpi[kpi_analytics_node<br/>fetches live KPIs via MCP]
    Hitl -->|skipped<br/>(dev mode)| Kpi
    Hitl -->|auto_blocked<br/>(critical risk)| END
    Hitl -->|rejected<br/>(revision < 3)| Creative
    Hitl -->|rejected<br/>(revision >= 3)| END
    Kpi --> Roi[roi_calculator_node<br/>produces RoiReport]
    Roi --> Opt[optimization_node<br/>max 3 suggestions]
    Opt --> END((END))

    classDef readonly fill:#e8f5e9,stroke:#2e7d32
    classDef gated fill:#fff3e0,stroke:#e65100
    classDef hitl fill:#fce4ec,stroke:#c2185b
    class Research,Creative,Media,Comply,Kpi,Roi,Opt readonly
    class Hitl hitl
```

### Edge routing rules

| From | Condition | To |
|------|-----------|----|
| START | always | research |
| research | always | creative_strategist |
| creative_strategist | always | media_planner |
| media_planner | always | compliance |
| compliance | always | human_review (interrupt) |
| human_review | reviewer approved | kpi_analytics |
| human_review | reviewer skipped (dev) | kpi_analytics |
| human_review | compliance critical risk | END (auto_blocked) |
| human_review | rejected, revision_count < 3 | creative_strategist (revise with comments) |
| human_review | rejected, revision_count >= 3 | END (blocked) |
| kpi_analytics | always | roi_calculator |
| roi_calculator | always | optimization |
| optimization | always | END |

---

## 3. End-to-end sequence (campaign run)

```mermaid
sequenceDiagram
    actor U as User
    participant UI as Streamlit
    participant API as FastAPI
    participant DB as Postgres
    participant Graph as LangGraph
    participant Agent as BaseAgent.run()
    participant LLM as LLM Provider
    participant MCP as MCP servers
    participant Audit as audit_events table

    U->>UI: Submit campaign brief
    UI->>API: POST /campaigns (X-AdForge-Tenant: acme)
    API->>DB: INSERT campaigns (status='draft')
    API-->>UI: 201 + campaign_id

    U->>UI: Click "Run agent graph"
    UI->>API: POST /campaigns/{id}/run
    API->>DB: UPDATE status='designing'
    API->>Graph: ainvoke(state, config={thread_id})
    Graph->>Agent: research_node.run(input)
    Agent->>Audit: AGENT_START event
    Agent->>LLM: chat(messages, tools=[])
    LLM-->>Agent: ResearchFindings JSON
    Agent->>MCP: (optional) call research tools
    Agent->>Audit: LLM_CALL event
    Agent->>DB: INSERT agent_runs (status='succeeded')
    Agent->>Audit: AGENT_END event
    Agent-->>Graph: research payload

    Graph->>Agent: creative_strategist (with upstream research)
    Note over Agent: same flow: validate → call LLM → parse → audit → persist
    Graph->>Agent: media_planner (with research + creative)
    Graph->>Agent: compliance (with creative + media_plan)
    Graph->>Graph: human_review_node → interrupt()

    Note over Graph,DB: GRAPH PAUSED. State persisted to Postgres checkpointer.

    API-->>UI: 200 + final_state (partial — waiting for review)
    UI-->>U: Show "Pending review" badge
```

---

## 4. HITL review sequence (approve / reject)

```mermaid
sequenceDiagram
    actor R as Reviewer
    participant UI as Streamlit Review Queue
    participant API as FastAPI /reviews
    participant Graph as LangGraph
    participant Perms as permissions module
    participant Ads as ads MCP
    participant Audit as audit_events

    R->>UI: Open Review Queue page
    UI->>API: GET /reviews/pending (X-AdForge-Tenant)
    API-->>UI: List of paused campaigns

    R->>UI: Select campaign, view creative + media_plan + verdict
    UI->>API: GET /reviews/{id}/packet
    API-->>UI: Review packet

    R->>UI: Click "Approve" + add comment
    UI->>API: POST /reviews/{id}/approve<br/>(X-AdForge-Reviewer: reviewer@acme.com)
    API->>API: Check reviewer in APP_REVIEWER_ALLOWLIST
    API->>Perms: issue_approval_token(campaign_id, "publish_campaign", reviewer)
    Perms-->>API: HMAC-signed token
    API->>Graph: ainvoke(Command(resume={approved: true, reviewer, comments}), config)
    Graph->>Graph: Route to kpi_analytics (skipping creative revision)
    Graph-->>API: final_state
    API->>Audit: INSERT compliance.decision event
    API-->>UI: 200 + final_state_keys

    Note over R,Ads: Later, when orchestrator wants to publish:

    Graph->>Ads: publish_campaign(channel, ...,<br/>X-AdForge-Approval-Token: <token>)
    Ads->>Perms: _verify_approval_token(token, campaign_id, "publish_campaign")
    Perms->>Perms: HMAC compare (constant-time)
    Perms-->>Ads: valid
    Ads-->>Graph: external_campaign_id (mock in v0.3)
```

### Reject path (with revision loop)

```mermaid
sequenceDiagram
    actor R as Reviewer
    participant UI as Streamlit
    participant API as FastAPI
    participant Graph as LangGraph

    R->>UI: Click "Reject" + add comments ("headline too generic")
    UI->>API: POST /reviews/{id}/reject (X-AdForge-Reviewer)
    API->>Graph: ainvoke(Command(resume={approved: false, comments}), config)
    Graph->>Graph: route_after_review → creative_strategist
    Graph->>Graph: revision_count += 1
    Note over Graph: creative_strategist re-runs with reviewer_comments in upstream
    Graph->>Graph: media_planner (re-evaluate budget with new creative)
    Graph->>Graph: compliance (re-check)
    Graph->>Graph: human_review interrupt again (if revision_count < 3)
    Graph-->>API: paused state (new interrupt)
    API-->>UI: 200 + new pending review
```

---

## 5. MCP server topology + permissions

```mermaid
flowchart TB
    subgraph Local["Local MCP servers (we run these)"]
        Res["research :9001<br/>web_search, serp_scrape,<br/>keyword_clusters,<br/>competitor_creatives,<br/>wikipedia_search"]
        Ads["ads :9002<br/>list_channels RO<br/>publish_campaign WRITE<br/>pause_campaign WRITE<br/>update_budget WRITE"]
        Ana["analytics :9003<br/>persist_kpi_snapshot RO<br/>query_kpis RO<br/>aggregate_kpis RO"]
        Roi["roi :9004<br/>compute_roas RO<br/>compute_cpa RO<br/>compute_payback RO<br/>classify RO"]
        Comp["compliance :9005<br/>evaluate_creative RO<br/>list_blocked_terms RO<br/>list_required_disclosures RO"]
        Pub["public_data :9007<br/>similarweb RO<br/>appstore_search RO<br/>appstore_reviews RO<br/>ipinfo_lookup RO<br/>abuseipdb_check RO<br/>github_repo_stats RO<br/>hackernews_top RO<br/>reddit_search RO<br/>stackexchange_search RO<br/>opencorporates_search RO<br/>wikipedia_search_direct RO"]
        Linked["linkedin :9012<br/>get_ad_analytics RO<br/>(custom — no official LinkedIn MCP exists)"]
    end

    subgraph External["External MCP servers (integrated, not run)"]
        GoogleOff["googleads/google-ads-mcp :9006<br/>official, read-only<br/>GAQL queries + customer list"]
        Pipeboard["Pipeboard remote MCPs<br/>Meta + Google + TikTok + Snap + Reddit<br/>OAuth via PIPEBOARD_API_TOKEN"]
    end

    subgraph Permissions["Permission decorators"]
        RO["@readonly — open to any agent"]
        W["@write_operation(requires_approval=True)<br/>requires X-AdForge-Approval-Token"]
    end

    Res --> RO
    Ana --> RO
    Roi --> RO
    Comp --> RO
    Pub --> RO
    Linked --> RO
    GoogleOff --> RO

    Ads -->|3 tools| W
    Ads -->|1 tool| RO

    style W fill:#ffcdd2,stroke:#b71c1c
    style RO fill:#c8e6c9,stroke:#1b5e20
```

### Tool inventory (v0.3)

| Server | Total tools | Read-only | Write-gated |
|--------|------------|-----------|-------------|
| research | 5 | 5 | 0 |
| ads | 4 | 1 (`list_channels`) | 3 (`publish_campaign`, `pause_campaign`, `update_budget`) |
| analytics | 3 | 3 | 0 |
| roi | 4 | 4 | 0 |
| compliance | 3 | 3 | 0 |
| public_data | 11 | 11 | 0 |
| linkedin | 1 | 1 | 0 |
| google-ads-mcp (official) | 3 | 3 | 0 |
| Pipeboard remote | ~230 | (per their policy) | (per their policy) |
| **TOTAL** | **~264** | **~261** | **3** |

**Only 3 of ~264 tools are write-capable, and all 3 require a valid HMAC-signed approval token.**

---

## 6. Permission / approval-token lifecycle

```mermaid
stateDiagram-v2
    [*] --> NoToken : Campaign started

    NoToken --> TokenIssued : HITL reviewer approves<br/>issue_approval_token() called
    NoToken --> Blocked : Agent calls @write_operation<br/>tool without token

    Blocked --> [*] : Returns {error: approval_required}

    TokenIssued --> TokenUsed : Orchestrator calls<br/>publish_campaign with token
    TokenIssued --> TokenExpired : TTL (1h) elapsed

    TokenUsed --> [*] : Tool executes<br/>(TODO v0.4: blacklist in Redis<br/>to enforce single-use)
    TokenExpired --> [*] : Subsequent calls blocked

    note right of TokenIssued
        Token format:
        <campaign_id>:<operation>:<expires_at>:<hmac>#<reviewer_email>
        HMAC computed over first 3 fields
        with APP_SECRET as key
    end note
```

```mermaid
flowchart LR
    subgraph Verify["_verify_approval_token()"]
        Split["Split token on #<br/>take signed part"]
        Parts["Split on :<br/>campaign_id, operation, expires, sig"]
        Expiry{"time.time() < expires?"}
        Scope{"campaign matches?<br/>operation matches?"}
        Hmac{"HMAC compare<br/>(constant time)"}
        Pass([return True])
        Fail([return False])
    end

    Split --> Parts --> Expiry
    Expiry -->|yes| Scope
    Expiry -->|no| Fail
    Scope -->|yes| Hmac
    Scope -->|no| Fail
    Hmac -->|match| Pass
    Hmac -->|no match| Fail
```

---

## 7. Multi-tenant data isolation

```mermaid
flowchart TB
    subgraph Request["Every API call"]
        Header["X-AdForge-Tenant: acme<br/>X-AdForge-Campaign: uuid"]
    end

    subgraph Scope["tenant_scope() context manager"]
        TenantScope["TenantScope<br/>(tenant_id, campaign_id)<br/>immutable dataclass"]
        Assert["assert_matches() — raises<br/>DataIsolationError if mismatch"]
        Headers["as_headers() — for MCP calls"]
        Cache["cache_key() — for Redis"]
    end

    subgraph Storage["Storage layer enforcement"]
        Query["SELECT * FROM ...<br/>WHERE tenant_id = :tid"]
        Row["Every row fetched is checked<br/>against scope.tenant_id"]
        Audit["audit_events row<br/>ALWAYS includes tenant_id"]
    end

    subgraph MCP["MCP server enforcement"]
        Header["X-AdForge-Tenant header<br/>REQUIRED on every call"]
        Reject["If header missing<br/>→ ValueError"]
    end

    Request --> Scope
    Scope --> Storage
    Scope --> MCP

    style Scope fill:#e3f2fd,stroke:#0d47a1
```

### Isolation guarantees

| Layer | Guarantee |
|-------|-----------|
| HTTP API | `X-AdForge-Tenant` header required on every call (returns 400 if missing) |
| LangGraph state | `tenant_id` + `campaign_id` set on first node, never mutated |
| BaseAgent.run() | Wraps every execution in `async with tenant_scope(...)` |
| Storage queries | Every SELECT filters `WHERE tenant_id = :tid` |
| MCP calls | Every call carries `X-AdForge-Tenant` header; server rejects if missing |
| Audit log | Every `audit_events` row includes `tenant_id` — can be queried per-tenant for compliance reviews |
| Approval tokens | HMAC includes campaign_id — token for tenant A's campaign cannot be used for tenant B |

---

## 8. LLM provider abstraction

```mermaid
flowchart TB
    subgraph Config["LLM_PROVIDER env var"]
        GLM["glm (default)"]
        OAI["openai"]
        ANT["anthropic"]
    end

    subgraph Factory["get_llm() — @lru_cache singleton"]
        Dispatch{"LLM_PROVIDER?"}
    end

    subgraph Adapters["Adapters implement LLMProvider"]
        ZaiGLM["ZaiGLMProvider<br/>https://open.bigmodel.cn/api/paas/v4<br/>OpenAI-compatible endpoint"]
        OpenAI["OpenAIProvider<br/>openai.AsyncOpenAI"]
        Anthropic["AnthropicProvider<br/>anthropic.AsyncAnthropic"]
    end

    subgraph Wrapper["LangChainLLMWrapper"]
        Base["BaseChatModel subclass<br/>enables create_agent(ReAct)"]
    end

    subgraph Agents["Agents call"]
        Single["Single-shot agents<br/>call llm.chat() directly"]
        React["ReAct agents (Research)<br/>use create_agent + ToolNode"]
    end

    Config --> Factory
    Dispatch -->|glm| ZaiGLM
    Dispatch -->|openai| OpenAI
    Dispatch -->|anthropic| Anthropic
    ZaiGLM --> Wrapper
    OpenAI --> Wrapper
    Anthropic --> Wrapper
    Wrapper --> React
    ZaiGLM --> Single
    OpenAI --> Single
    Anthropic --> Single

    style Factory fill:#fff9c4,stroke:#f57f17
```

---

## 9. Web-search provider chain (Tavily-free)

```mermaid
flowchart LR
    Query["web_search(query)"] --> DDG{DuckDuckGo<br/>HTML scrape<br/>FREE, no key}
    DDG -->|200 + results| Return1["return results<br/>provider=duckduckgo"]
    DDG -->|timeout / blocked| Wiki{Wikipedia<br/>opensearch API<br/>FREE, no key}
    Wiki -->|200 + results| Return2["return results<br/>provider=wikipedia"]
    Wiki -->|fail| Tavily{TAVILY_API_KEY<br/>configured?}
    Tavily -->|yes| TavilyAPI["call Tavily API<br/>paid fallback"]
    Tavily -->|no| Mock["return mock results<br/>so agent doesn't crash"]
    TavilyAPI -->|200| Return3["return results<br/>provider=tavily"]
    TavilyAPI -->|error| Mock

    style DDG fill:#c8e6c9,stroke:#1b5e20
    style Wiki fill:#c8e6c9,stroke:#1b5e20
    style TavilyAPI fill:#fff3e0,stroke:#e65100
    style Mock fill:#ffcdd2,stroke:#b71c1c
```

### Free public-data tool inventory

| Tool | API | Auth required | Rate limit |
|------|-----|---------------|------------|
| `web_search` (DDG path) | html.duckduckgo.com | None | None documented |
| `wikipedia_search` | en.wikipedia.org/w/api.php | None | Generous |
| `hackernews_top` | hacker-news.firebaseio.com | None | Unlimited |
| `reddit_search` | reddit.com/r/.../search.json | None (read-only) | ~60/min/IP |
| `stackexchange_search` | api.stackexchange.com | None | 300/sec no key |
| `appstore_search` | itunes.apple.com/search | None | ~20/min/IP |
| `appstore_reviews` | itunes.apple.com/.../rss | None | Generous |
| `opencorporates_search` | api.opencorporates.com | None | 100/day |
| `github_repo_stats` | api.github.com/repos | None (60/hr) or token (5000/hr) | 60/hr/IP |
| `ipinfo_lookup` | ipinfo.io | Optional token | 50k/mo free |
| `abuseipdb_check` | api.abuseipdb.com | Key required | 1k/day free |
| `similarweb_traffic` | api.similarweb.com (via Composio) | Key required | Free tier |

**Net result: the Research agent can run end-to-end with ZERO paid credentials.**

---

## 10. Storage layer + checkpointing

```mermaid
erDiagram
    campaigns ||--o{ agent_runs : "1 to many"
    campaigns ||--o{ kpi_snapshots : "1 to many"
    campaigns ||--o{ creative_artifacts : "1 to many"
    campaigns ||--o{ audit_events : "1 to many (via campaign_id)"

    campaigns {
        uuid id PK
        string tenant_id
        string name
        jsonb brief
        string status
        float total_spend_usd
        float total_revenue_usd
        timestamptz started_at
        timestamptz ended_at
        timestamptz created_at
        timestamptz updated_at
    }

    agent_runs {
        uuid id PK
        string tenant_id
        uuid campaign_id FK
        uuid request_id
        string agent_name
        jsonb input_payload
        jsonb output_payload
        jsonb usage
        string trace_id
        bool approved_by_compliance
        text error
        string status
        timestamptz started_at
        timestamptz finished_at
    }

    kpi_snapshots {
        uuid id PK
        string tenant_id
        uuid campaign_id FK
        string channel
        string campaign_external_id
        int impressions
        int clicks
        float spend_usd
        int conversions
        float revenue_usd
        float ctr
        float cvr
        float cpc
        float cpa
        float cpm
        float roas
        timestamptz captured_at
    }

    creative_artifacts {
        uuid id PK
        string tenant_id
        uuid campaign_id FK
        string agent_name
        string channel
        string headline
        text primary_text
        string cta
        text visual_direction
        jsonb ab_variants
        bool approved_by_compliance
    }

    audit_events {
        uuid event_id PK
        string event_type
        string tenant_id
        uuid campaign_id
        string agent_name
        uuid request_id
        jsonb payload
        timestamptz occurred_at
    }
```

### Append-only invariant

```mermaid
flowchart LR
    AgentCall["Agent executes"] --> Emit["Emit AuditEvent"]
    Emit --> Buffer["Buffer in memory"]
    Buffer --> Flush["flush() — raw SQL INSERT"]
    Flush --> Table["audit_events table"]
    Note["NO UPDATE PATH<br/>NO DELETE PATH<br/>Raw SQL — no ORM model<br/>that could allow mutation"]

    style Note fill:#ffcdd2,stroke:#b71c1c
```

### LangGraph checkpointer

```mermaid
flowchart LR
    Node["graph node executes"] --> State["StateGraph state update"]
    State --> Check["AsyncPostgresSaver.aput(config, state)"]
    Check --> Postgres[("Postgres<br/>checkpoint_blobs table")]
    Postgres --> Resume["On graph resume,<br/>AsyncPostgresSaver.aget(config)"]
    Resume --> Continue["graph continues from<br/>exact paused state"]

    style Postgres fill:#e3f2fd,stroke:#0d47a1
```

---

## 11. Docker-compose deployment topology

```mermaid
flowchart TB
    subgraph Host["Docker host"]
        subgraph Infra["Infrastructure"]
            PG[("postgres:16-alpine<br/>:5432")]
            RD[("redis:7-alpine<br/>:6379")]
        end

        subgraph MCS["MCP servers (one process each)"]
            M1["mcp-research :9001"]
            M2["mcp-ads :9002"]
            M3["mcp-analytics :9003"]
            M4["mcp-roi :9004"]
            M5["mcp-compliance :9005"]
            M6["mcp-public-data :9007"]
            M7["mcp-google-ads-official :9006<br/>(image: ghcr.io/googleads/google-ads-mcp)"]
        end

        subgraph App["Application"]
            API["api :8000<br/>(FastAPI + uvicorn)"]
            UI["dashboard :8501<br/>(Streamlit)"]
        end
    end

    ExternalPipeboard["Pipeboard remote MCPs<br/>https://*.mcp.pipeboard.co/sse"]
    ExternalAPIs["External APIs<br/>Tavily / SimilarWeb /<br/>IPInfo / AbuseIPDB /<br/>HN / Reddit / SO /<br/>OpenCorporates / iTunes"]

    API --> PG & RD
    API --> M1 & M2 & M3 & M4 & M5 & M6 & M7
    UI --> API
    M1 --> ExternalAPIs
    M6 --> ExternalAPIs
    M2 --> ExternalPipeboard

    style M7 fill:#e8f5e9,stroke:#2e7d32
    style ExternalPipeboard fill:#fff3e0,stroke:#e65100
    style ExternalAPIs fill:#f3e5f5,stroke:#4a148c
```

### Why this topology

| Decision | Reason |
|----------|--------|
| One container per MCP server | Distributes load — no single process carries the full tool surface |
| Postgres + Redis as separate containers | Standard managed equivalents in prod (RDS / ElastiCache) |
| Streamlit dashboard is single-container | Dev tool; replace with Next.js in prod |
| google-ads-mcp uses pre-built image | Avoids vendoring Google's code |
| Pipeboard MCPs are remote (not local containers) | OAuth handled by Pipeboard; we just call their SSE endpoint |

---

## 12. Feature-based repo structure

```mermaid
flowchart TB
    subgraph Root["adforge/"]
        subgraph Pkg["adforge/ (Python package)"]
            Config["config.py<br/>pydantic-settings"]
            subgraph AgentsDir["agents/"]
                Base["base.py — BaseAgent"]
                subgraph A1["research/"]
                    A1a["agent.py<br/>react_agent.py"]
                    A1b["persona.py"]
                    A1c["tools.py"]
                end
                subgraph A2["creative_strategist/"]
                    A2a["agent.py"]
                    A2b["persona.py"]
                end
                subgraph A3["media_planner/"]
                    A3a["agent.py"]
                    A3b["persona.py"]
                end
                subgraph A4["kpi_analytics/"]
                    A4a["agent.py"]
                    A4b["persona.py"]
                    A4c["fetcher.py"]
                end
                subgraph A5["roi_calculator/"]
                    A5a["agent.py"]
                    A5b["persona.py"]
                end
                subgraph A6["compliance/"]
                    A6a["agent.py"]
                    A6b["persona.py"]
                end
                subgraph A7["optimization/"]
                    A7a["agent.py"]
                    A7b["persona.py"]
                end
            end
            subgraph Guard["guardrails/"]
                Contracts["contracts.py"]
                Iso["data_isolation.py"]
                RL["rate_limiter.py"]
                Audit["audit_log.py"]
                Policy["policy.py"]
                Rules["policy_rules.yaml"]
                Exc["exceptions.py"]
            end
            subgraph Stor["storage/"]
                Models["models.py"]
                Sess["session.py"]
                Redis2["redis_client.py"]
                Seed["seed.py"]
                Alemb["alembic_env.py"]
            end
            subgraph LLM["llm/"]
                Factory["factory.py"]
                GLM["zai_glm.py"]
                OAI["openai_provider.py"]
                ANT["anthropic_provider.py"]
                Wrap["langchain_wrapper.py"]
            end
            subgraph MCSPkg["mcp_servers/"]
                Run["runner.py"]
                Perms["permissions.py"]
                Client["client.py"]
                Res2["research.py"]
                Ads2["ads/__init__.py<br/>ads/linkedin.py"]
                Ana2["analytics.py"]
                Roi2["roi.py"]
                Comp2["compliance.py"]
                Pub2["public_data.py"]
            end
            subgraph GraphDir["graph/"]
                State["state.py"]
            end
            subgraph APIPkg["api/"]
                Main["main.py"]
                Deps["deps.py"]
                Routes["routes/<br/>campaigns.py<br/>kpis.py<br/>reviews.py<br/>traces.py"]
            end
            subgraph Dash["dashboard/"]
                App2["app.py"]
                Pages["pages/<br/>1_Campaigns.py<br/>2_Agent_Traces.py<br/>3_ROI_Calculator.py<br/>4_Review_Queue.py"]
            end
        end
        subgraph Tests["tests/"]
            TG["test_guardrails.py"]
            TA["test_agents.py"]
            TM["test_mcp_servers.py"]
            TL["test_llm.py"]
            TAG["test_api_and_graph.py"]
            TV2["test_v02_additions.py"]
        end
        subgraph Docs["docs/"]
            Arch["ARCHITECTURE.md"]
            Audit2["HONEST_AUDIT.md"]
            Res3["RESEARCH.md"]
            Add["ADDING_AN_AGENT.md"]
            Design["DESIGN.md (this file)"]
            Gob["GOBBLECUBE_PLAN.md"]
        end
        subgraph CI["CI / infra"]
            CI2[".github/workflows/ci.yml"]
            Compose["docker-compose.yml"]
            Docker["Dockerfile"]
            Make["Makefile"]
            EnvEx[".env.example"]
        end
    end
```

### Folder conventions

| Folder | Rule |
|--------|------|
| `adforge/agents/<name>/` | One folder per agent. Adding an agent = adding a folder. See `ADDING_AN_AGENT.md`. |
| `adforge/mcp_servers/` | One file per MCP server. AdForge-local servers only. |
| `adforge/guardrails/` | Imported by every other module. Never imports from agents/ or storage/. |
| `adforge/storage/` | ORM models + session. Imports from guardrails/ for tenant mixin. |
| `adforge/llm/` | Provider adapters. Never imports from agents/. |
| `adforge/graph/` | The compiled graph. Imports agents/. Agents never import graph/. |
| `adforge/api/routes/` | One file per resource. Routes import graph + storage + guardrails. |
| `adforge/dashboard/pages/` | Streamlit multipage convention. Each file = one sidebar entry. |

---

## 13. GobbleCube plan summary

> Full plan: [`docs/GOBBLECUBE_PLAN.md`](GOBBLECUBE_PLAN.md)

```mermaid
flowchart LR
    subgraph GobbleCube["GobbleCube product suite"]
        GE["Gobbs Edge<br/>cross-platform diagnostics"]
        GB["Gobbs Boost<br/>goal-based campaigns"]
        GF["Gobbs Flow<br/>availability lifecycle"]
        GD["Gobbs Discover<br/>micro-category trends"]
        PB["Playbook<br/>growth recipes"]
        GCI["GC Incubator<br/>D2C onboarding"]
    end

    subgraph AdForge["AdForge agents + new modules"]
        Research["ResearchAgent"]
        Creative["CreativeStrategist"]
        Media["MediaPlanner"]
        Comply["Compliance"]
        Kpi["KpiAnalytics"]
        Roi["RoiCalculator"]
        Opt["Optimization"]
        New1["NEW: InventoryAgent<br/>(maps to Gobbs Flow)"]
        New2["NEW: TrendSpotter<br/>(maps to Gobbs Discover)"]
        New3["NEW: DiagnosticsAgent<br/>(maps to Gobbs Edge)"]
        New4["NEW: PlaybookEngine<br/>(maps to Playbook)"]
    end

    GE --> New3
    GB --> Media & Roi & Opt
    GF --> New1
    GD --> New2
    PB --> New4
    GCI --> New3 & New2

    style New1 fill:#e1bee7,stroke:#4a148c
    style New2 fill:#e1bee7,stroke:#4a148c
    style New3 fill:#e1bee7,stroke:#4a148c
    style New4 fill:#e1bee7,stroke:#4a148c
```

See `docs/GOBBLECUBE_PLAN.md` for the full mapping + 8-week build plan + new MCPs needed (e-commerce platform integrations, stock-availability APIs).
