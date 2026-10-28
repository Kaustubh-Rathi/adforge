"""Shared Streamlit helpers — talk to the AdForge API."""
from __future__ import annotations

import os
from typing import Any

import httpx
import pandas as pd

API_BASE = os.getenv("ADFORGE_API_URL", "http://localhost:8000")
TENANT = os.getenv("APP_TENANT_ID", "default_tenant")


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    h = {"X-AdForge-Tenant": TENANT}
    if extra:
        h.update(extra)
    return h


def list_campaigns() -> list[dict[str, Any]]:
    r = httpx.get(f"{API_BASE}/campaigns", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_campaign(campaign_id: str) -> dict[str, Any]:
    r = httpx.get(f"{API_BASE}/campaigns/{campaign_id}", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def create_campaign(name: str, brief: dict[str, Any]) -> dict[str, Any]:
    r = httpx.post(
        f"{API_BASE}/campaigns",
        json={"name": name, "brief": brief},
        headers=_headers(),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def run_campaign(campaign_id: str) -> dict[str, Any]:
    r = httpx.post(
        f"{API_BASE}/campaigns/{campaign_id}/run",
        headers=_headers({"X-AdForge-Campaign": campaign_id}),
        timeout=300,  # 5 min — agent graph can be slow
    )
    r.raise_for_status()
    return r.json()


def get_kpis(campaign_id: str, days_back: int = 30, channel: str | None = None) -> dict[str, Any]:
    params = {"days_back": days_back}
    if channel:
        params["channel"] = channel
    r = httpx.get(
        f"{API_BASE}/kpis/{campaign_id}/snapshots",
        params=params, headers=_headers(), timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_aggregated_kpis(campaign_id: str, group_by: str = "channel") -> dict[str, Any]:
    r = httpx.get(
        f"{API_BASE}/kpis/{campaign_id}/aggregate",
        params={"group_by": group_by},
        headers=_headers(), timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_agent_runs(campaign_id: str, agent_name: str | None = None) -> dict[str, Any]:
    params = {}
    if agent_name:
        params["agent_name"] = agent_name
    r = httpx.get(
        f"{API_BASE}/traces/{campaign_id}/runs",
        params=params, headers=_headers(), timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_audit_events(campaign_id: str, event_type: str | None = None) -> dict[str, Any]:
    params = {}
    if event_type:
        params["event_type"] = event_type
    r = httpx.get(
        f"{API_BASE}/traces/{campaign_id}/audit",
        params=params, headers=_headers(), timeout=10,
    )
    r.raise_for_status()
    return r.json()


# --- v0.2: HITL review endpoints ---

def list_pending_reviews() -> dict[str, Any]:
    r = httpx.get(f"{API_BASE}/reviews/pending", headers=_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def get_review_packet(campaign_id: str) -> dict[str, Any]:
    r = httpx.get(
        f"{API_BASE}/reviews/{campaign_id}/packet", headers=_headers(), timeout=10,
    )
    r.raise_for_status()
    return r.json()


def approve_campaign(campaign_id: str, comments: str, reviewer_email: str) -> dict[str, Any]:
    r = httpx.post(
        f"{API_BASE}/reviews/{campaign_id}/approve",
        json={"approved": True, "comments": comments},
        headers=_headers({"X-AdForge-Reviewer": reviewer_email}),
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def reject_campaign(campaign_id: str, comments: str, reviewer_email: str) -> dict[str, Any]:
    r = httpx.post(
        f"{API_BASE}/reviews/{campaign_id}/reject",
        json={"approved": False, "comments": comments},
        headers=_headers({"X-AdForge-Reviewer": reviewer_email}),
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def kpis_to_df(kpis: dict[str, Any]) -> pd.DataFrame:
    """Convert a kpis response to a pandas DataFrame."""
    rows = kpis.get("snapshots", [])
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "captured_at" in df.columns:
        df["captured_at"] = pd.to_datetime(df["captured_at"])
    return df
