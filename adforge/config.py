"""Centralised, validated configuration for AdForge.

All runtime configuration is loaded from environment variables through
pydantic-settings. Guardrails (rate limits, write gates, isolation) are first-class
settings — disabling them is impossible in prod-like environments.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings. Loaded once at startup; cached via ``lru_cache``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    app_env: Literal["dev", "test", "staging", "prod"] = "dev"
    app_log_level: str = "INFO"
    app_tenant_id: str = "default_tenant"
    app_disable_guardrails: bool = False
    app_secret: str = ""  # HMAC key for approval tokens. MUST be set in prod.
    app_reviewer_allowlist: str = ""  # comma-separated emails authorised to approve writes
    app_cors_origins: str = "http://localhost:8501,http://127.0.0.1:8501"
    # Shared API key. Required in staging/prod; optional in dev/test.
    api_auth_token: str = ""
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # --- LLM ---
    # "opencode" = OpenAI-compatible endpoint (e.g. opencode.ai free tier)
    llm_provider: Literal["glm", "openai", "anthropic", "opencode"] = "glm"
    zai_api_key: str = ""
    zai_model: str = "glm-4.6"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    # Generic OpenAI-compatible endpoint. Used by LLM_PROVIDER=opencode.
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout: float = 180.0
    llm_temperature: float = 0.4
    llm_max_tokens: int = 2048

    # --- LangGraph checkpointer ---
    # postgres: durable HITL resume (requires Postgres)
    # memory:   in-process resume (dev/test only)
    # none:     no persistence (HITL cannot resume)
    checkpointer_backend: Literal["postgres", "memory", "none"] = "postgres"

    # --- Ad platform API versions / endpoints ---
    # Endpoints are configurable so tests and self-hosted gateways can point
    # at fixture servers without changing production code.
    google_ads_api_version: str = "v21"
    google_ads_api_base: str = "https://googleads.googleapis.com"
    google_oauth_token_url: str = "https://oauth2.googleapis.com/token"
    meta_graph_api_version: str = "v21.0"
    meta_graph_base: str = "https://graph.facebook.com"
    linkedin_api_base: str = "https://api.linkedin.com"
    linkedin_api_version: str = "202411"
    tiktok_api_version: str = "v1.3"
    tiktok_api_base: str = "https://business-api.tiktok.com"

    # --- External HTTP clients ---
    # Every outbound HTTP client uses these unless a provider needs a longer
    # timeout. Endpoints are configurable so tests can point them at local
    # fixture servers and self-hosted gateways need no code changes.
    external_http_timeout: float = 30.0
    external_user_agent: str = "AdForge/0.4 (research-agent)"
    research_search_url: str = "https://html.duckduckgo.com/html/"
    wikipedia_api_url: str = "https://en.wikipedia.org/w/api.php"
    tavily_base_url: str = "https://api.tavily.com"
    google_suggest_url: str = "https://suggestqueries.google.com/complete/search"
    similarweb_api_base: str = "https://api.similarweb.com"
    itunes_api_base: str = "https://itunes.apple.com"
    ipinfo_api_base: str = "https://ipinfo.io"
    abuseipdb_api_base: str = "https://api.abuseipdb.com/api/v2"
    github_api_base: str = "https://api.github.com"
    hackernews_api_base: str = "https://hacker-news.firebaseio.com/v0"
    reddit_api_base: str = "https://www.reddit.com"
    stackexchange_api_base: str = "https://api.stackexchange.com/2.3"
    hn_algolia_api_base: str = "https://hn.algolia.com/api/v1"
    opencorporates_api_base: str = "https://api.opencorporates.com/v0.4"
    producthunt_api_url: str = "https://api.producthunt.com/v2/api/graphql"
    producthunt_api_token: str = ""
    google_trends_base_url: str = "https://trends.google.com"
    jina_reader_url: str = "https://r.jina.ai"
    sec_edgar_api_base: str = "https://data.sec.gov/api"
    wikimedia_rest_api_base: str = "https://wikimedia.org/api/rest_v1"
    zai_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_base_url: str = "https://api.anthropic.com"
    dashboard_api_url: str = "http://localhost:8000"
    dashboard_http_timeout: float = 30.0
    dashboard_run_timeout: float = 300.0
    mcp_bind_host: str = "127.0.0.1"
    mcp_bind_port: int = 9000

    # --- Write safety ---
    # Publishing/pausing/budget changes are disabled unless an operator
    # explicitly enables them. Even then they require a single-use HITL token.
    adforge_enable_live_writes: bool = False
    adforge_publish_webhook_url: str = ""
    adforge_publish_webhook_token: str = ""
    guardrails_max_campaign_budget_usd: float = 1_000_000.0

    # --- Postgres ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "adforge"
    postgres_password: str = "adforge_dev_password"
    postgres_db: str = "adforge"
    database_url: str = ""

    # --- Redis ---
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_password: str = ""
    redis_url: str = ""

    # --- MCP ---
    mcp_enabled: bool = True
    mcp_transport: Literal["stdio", "sse", "streamable_http"] = "stdio"
    mcp_research_port: int = 9001
    mcp_ads_port: int = 9002
    mcp_analytics_port: int = 9003
    mcp_roi_port: int = 9004
    mcp_compliance_port: int = 9005
    mcp_google_ads_official_port: int = 9006
    mcp_public_data_port: int = 9007
    mcp_linkedin_port: int = 9012
    # Host used when constructing local MCP URLs. In Docker Compose the API
    # container uses service-name URLs via the per-server overrides below.
    mcp_host: str = "localhost"
    mcp_scheme: Literal["http", "https"] = "http"
    mcp_research_url: str = ""
    mcp_ads_url: str = ""
    mcp_analytics_url: str = ""
    mcp_roi_url: str = ""
    mcp_compliance_url: str = ""
    mcp_google_ads_official_url: str = ""
    mcp_public_data_url: str = ""
    mcp_linkedin_url: str = ""

    # --- Public data API keys (optional; tools return explicit config errors) ---
    similarweb_api_key: str = ""
    ipinfo_api_token: str = ""
    abuseipdb_api_key: str = ""
    # GitHub token — raises API rate limit from 60/hr/IP to 5000/hr. Optional.
    github_token: str = ""
    # App Store uses Apple's public iTunes endpoint — no key needed.
    # DuckDuckGo, Wikipedia, HN, Reddit, StackExchange, OpenCorporates — no key needed.
    # Tavily remains as optional paid fallback for higher-quality search.
    tavily_api_key: str = ""

    # --- External MCP integrations (see docs/RESEARCH.md) ---
    pipeboard_api_token: str = ""
    pipeboard_meta_url: str = "https://meta-ads.mcp.pipeboard.co/sse"
    pipeboard_tiktok_url: str = "https://tiktok-ads.mcp.pipeboard.co/sse"
    pipeboard_snap_url: str = "https://snap-ads.mcp.pipeboard.co/sse"
    pipeboard_reddit_url: str = "https://reddit-ads.mcp.pipeboard.co/sse"

    google_project_id: str = ""
    google_application_credentials: str = ""

    # --- Ad platforms ---
    # Simulated KPI data path. When app_env is dev/test and the channel flag
    # is on, the KPI fetcher returns deterministic mock snapshots (labelled
    # is_mock=true) instead of calling the platform API. Staging/prod always
    # use the real APIs regardless of these flags.
    google_ads_use_mock: bool = True
    meta_use_mock: bool = True
    linkedin_use_mock: bool = True
    tiktok_use_mock: bool = True

    google_ads_developer_token: str = ""
    google_ads_client_id: str = ""
    google_ads_client_secret: str = ""
    google_ads_refresh_token: str = ""
    google_ads_customer_id: str = ""

    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_access_token: str = ""
    meta_ad_account_id: str = ""
    meta_ad_library_token: str = ""

    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    linkedin_access_token: str = ""
    linkedin_ad_account_id: str = ""

    tiktok_app_id: str = ""
    tiktok_app_secret: str = ""
    tiktok_access_token: str = ""
    tiktok_advertiser_id: str = ""

    # --- LangSmith / tracing ---
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "adforge-dev"

    # --- Guardrails ---
    guardrails_max_llm_calls_per_campaign: int = 200
    guardrails_max_api_calls_per_min: int = 60
    guardrails_require_compliance_approval: bool = True

    # --- Agent tool loop ---
    # Every LLM-facing tool is read-only. Write tools are orchestrator-only and
    # gated by a single-use HITL approval token.
    agent_max_tool_iterations: int = 8
    agent_max_tool_result_chars: int = 16_000

    @field_validator("database_url")
    @classmethod
    def _default_db_url(cls, v: str, info) -> str:
        if v:
            return v
        data = info.data
        return (
            f"postgresql+asyncpg://{data.get('postgres_user', 'adforge')}:"
            f"{data.get('postgres_password', 'adforge_dev_password')}@"
            f"{data.get('postgres_host', 'localhost')}:"
            f"{data.get('postgres_port', 5432)}/{data.get('postgres_db', 'adforge')}"
        )

    @field_validator("redis_url")
    @classmethod
    def _default_redis_url(cls, v: str, info) -> str:
        if v:
            return v
        data = info.data
        pwd = data.get("redis_password", "")
        auth = f":{pwd}@" if pwd else ""
        return f"redis://{auth}{data.get('redis_host', 'localhost')}:{data.get('redis_port', 6379)}/0"

    @property
    def in_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def guardrails_active(self) -> bool:
        """Guardrails cannot be disabled in staging/prod."""
        if self.in_prod or self.app_env == "staging":
            return True
        return not self.app_disable_guardrails

    @model_validator(mode="after")
    def _production_safety(self) -> Settings:
        """Refuse to start a staging/prod deployment with unsafe defaults."""
        if self.app_env not in ("staging", "prod"):
            return self
        missing = []
        if not self.app_secret:
            missing.append("APP_SECRET")
        if not self.app_reviewer_allowlist.strip():
            missing.append("APP_REVIEWER_ALLOWLIST")
        if not self.api_auth_token:
            missing.append("API_AUTH_TOKEN")
        if not self.guardrails_require_compliance_approval:
            missing.append("GUARDRAILS_REQUIRE_COMPLIANCE_APPROVAL=true")
        if self.checkpointer_backend != "postgres":
            missing.append("CHECKPOINTER_BACKEND=postgres")
        if self.app_disable_guardrails:
            missing.append("APP_DISABLE_GUARDRAILS=false")
        if self.app_cors_origins.strip() == "*":
            missing.append("APP_CORS_ORIGINS (explicit origins)")
        if missing:
            raise ValueError(
                "Unsafe production configuration: " + ", ".join(missing)
            )
        return self

    def mcp_url(self, server: Literal["research", "ads", "analytics", "roi", "compliance", "google_ads_official", "public_data", "linkedin"]) -> str:
        overrides = {
            "research": self.mcp_research_url,
            "ads": self.mcp_ads_url,
            "analytics": self.mcp_analytics_url,
            "roi": self.mcp_roi_url,
            "compliance": self.mcp_compliance_url,
            "google_ads_official": self.mcp_google_ads_official_url,
            "public_data": self.mcp_public_data_url,
            "linkedin": self.mcp_linkedin_url,
        }
        if overrides[server]:
            return overrides[server]
        port_map = {
            "research": self.mcp_research_port,
            "ads": self.mcp_ads_port,
            "analytics": self.mcp_analytics_port,
            "roi": self.mcp_roi_port,
            "compliance": self.mcp_compliance_port,
            "google_ads_official": self.mcp_google_ads_official_port,
            "public_data": self.mcp_public_data_port,
            "linkedin": self.mcp_linkedin_port,
        }
        return f"{self.mcp_scheme}://{self.mcp_host}:{port_map[server]}/sse"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance. Use this everywhere — never call Settings() directly."""
    return Settings()
