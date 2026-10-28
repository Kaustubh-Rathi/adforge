"""Centralised, validated configuration for AdForge.

All runtime configuration is loaded from environment variables through
pydantic-settings. Guardrails (rate limits, mock flags, isolation) are first-class
settings — disabling them is impossible in prod-like environments.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
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

    # --- LLM ---
    llm_provider: Literal["glm", "openai", "anthropic"] = "glm"
    zai_api_key: str = ""
    zai_model: str = "glm-4.6"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    llm_temperature: float = 0.4
    llm_max_tokens: int = 2048

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
    mcp_transport: Literal["stdio", "sse", "streamable_http"] = "stdio"
    mcp_research_port: int = 9001
    mcp_ads_port: int = 9002
    mcp_analytics_port: int = 9003
    mcp_roi_port: int = 9004
    mcp_compliance_port: int = 9005
    mcp_google_ads_official_port: int = 9006
    mcp_public_data_port: int = 9007

    # --- Public data API keys (all optional — fall back to mock mode when blank) ---
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
    google_ads_developer_token: str = ""
    google_ads_client_id: str = ""
    google_ads_client_secret: str = ""
    google_ads_refresh_token: str = ""
    google_ads_customer_id: str = ""
    google_ads_use_mock: bool = True

    meta_app_id: str = ""
    meta_app_secret: str = ""
    meta_access_token: str = ""
    meta_ad_account_id: str = ""
    meta_use_mock: bool = True

    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    linkedin_access_token: str = ""
    linkedin_ad_account_id: str = ""
    linkedin_use_mock: bool = True

    tiktok_app_id: str = ""
    tiktok_app_secret: str = ""
    tiktok_access_token: str = ""
    tiktok_advertiser_id: str = ""
    tiktok_use_mock: bool = True

    # --- LangSmith / tracing ---
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "adforge-dev"

    # --- Guardrails ---
    guardrails_max_llm_calls_per_campaign: int = 200
    guardrails_max_api_calls_per_min: int = 60
    guardrails_require_compliance_approval: bool = True

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

    def mcp_url(self, server: Literal["research", "ads", "analytics", "roi", "compliance", "google_ads_official", "public_data"]) -> str:
        port_map = {
            "research": self.mcp_research_port,
            "ads": self.mcp_ads_port,
            "analytics": self.mcp_analytics_port,
            "roi": self.mcp_roi_port,
            "compliance": self.mcp_compliance_port,
            "google_ads_official": self.mcp_google_ads_official_port,
            "public_data": self.mcp_public_data_port,
        }
        return f"http://localhost:{port_map[server]}/sse"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance. Use this everywhere — never call Settings() directly."""
    return Settings()
