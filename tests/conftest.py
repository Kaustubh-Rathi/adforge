"""Pytest fixtures shared across all tests."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

# Ensure the package is importable when running tests from a fresh checkout.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Force test-mode env before any adforge import.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LLM_PROVIDER", "glm")
os.environ.setdefault("ZAI_API_KEY", "test-key-not-real")
os.environ.setdefault("APP_DISABLE_GUARDRAILS", "false")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://adforge:adforge_dev_password@localhost:5432/adforge_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("GOOGLE_ADS_USE_MOCK", "true")
os.environ.setdefault("META_USE_MOCK", "true")
os.environ.setdefault("LINKEDIN_USE_MOCK", "true")
os.environ.setdefault("TIKTOK_USE_MOCK", "true")


@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for the whole session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def sample_brief_dict():
    return {
        "product_name": "Acme SaaS",
        "product_description": "Project management tool for fast-moving dev teams. Tight integrations, instant sprint burndowns, AI-assisted standup summaries.",
        "target_audience": "Engineering managers at 50-500 person SaaS companies in North America.",
        "budget_usd": 25000.0,
        "duration_days": 30,
        "business_objective": "conversion",
        "channels_hint": ["google", "linkedin"],
        "constraints": ["No comparison-brand keyword bidding"],
    }
