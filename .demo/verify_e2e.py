"""End-to-end verification: create -> run -> HITL approve -> KPI/ROI/optimization.

Run against a live API on :8000 with Postgres/Redis from docker-compose.
Prints a compact pass/fail summary; exits non-zero on failure.

Every run uses a throwaway tenant (``verify_tenant``) and deletes its
campaigns afterwards, so verifying the demo never litters the demo DB.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
TENANT = "verify_tenant"
HDR = {"X-AdForge-Tenant": TENANT, "Content-Type": "application/json"}

failures: list[str] = []


def call(method: str, path: str, body=None, timeout: int = 900, extra=None):
    headers = dict(HDR)
    if extra:
        headers.update(extra)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw[:1500]


def check(label: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + label + (f" :: {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [part for part in value.replace(",", " ").split() if part.strip()]
    return []


def _cleanup() -> None:
    """Remove this script's throwaway campaigns (best effort, no API to delete)."""
    try:
        import asyncio

        from sqlalchemy import text

        from adforge.storage import session as session_module

        async def _run() -> int:
            session_module.init_engine()
            factory = session_module.get_session_factory()
            async with factory() as session:
                rows = await session.execute(
                    text("SELECT id FROM campaigns WHERE tenant_id = :t"), {"t": TENANT}
                )
                ids = [str(r.id) for r in rows.fetchall()]
                if ids:
                    for table in ("kpi_snapshots", "agent_runs", "creative_artifacts"):
                        await session.execute(
                            text(f"DELETE FROM {table} WHERE campaign_id = ANY(:ids)"),
                            {"ids": ids},
                        )
                    await session.execute(
                        text("DELETE FROM campaigns WHERE id = ANY(:ids)"), {"ids": ids}
                    )
                    await session.commit()
            await session_module.dispose_engine()
            return len(ids)

        removed = asyncio.run(_run())
        print(f"(cleaned up {removed} verification campaign(s))")
    except Exception as exc:  # never fail a verification run over cleanup
        print(f"(cleanup skipped: {exc})")


brief = {
    "product_name": "Atlas Analytics",
    "product_description": (
        "Privacy-first product analytics for B2B SaaS teams, with "
        "warehouse-native dashboards."
    ),
    "target_audience": (
        "Product managers and data leads at 50-500 person B2B SaaS companies "
        "in North America."
    ),
    "budget_usd": 15000.0,
    "duration_days": 14,
    "channels_hint": ["google", "linkedin"],
    "business_objective": "conversion",
    "constraints": [
        "No comparison-brand keyword bidding.",
        "Must disclose pricing on the landing page.",
    ],
}

print("== 1. create campaign")
status, created = call("POST", "/campaigns", {"name": "Verify - Atlas launch", "brief": brief})
check("POST /campaigns", status in (200, 201), f"status={status} {str(created)[:200]}")
if status not in (200, 201):
    sys.exit(1)
cid = created.get("id") or created.get("campaign_id")
print(f"     campaign_id={cid}")

print("== 2. run agent graph (pauses at the HITL gate)")
status, run = call("POST", f"/campaigns/{cid}/run", {})
check("POST /campaigns/{id}/run", status in (200, 201), f"status={status}")
if status not in (200, 201):
    print(json.dumps(run, indent=2)[:3000] if isinstance(run, dict) else run)
    sys.exit(1)

fs = run.get("final_state") or {}
paused = run.get("paused_for_review")
check("run paused for human review", paused is True, f"status={run.get('status')}")
check("no run errors", not fs.get("errors"), str(fs.get("errors"))[:300])

for key in ("research", "creative", "media_plan", "compliance_verdict"):
    check(f"upstream payload present: {key}", bool(fs.get(key)))

research = fs.get("research") or {}
sources = as_list(research.get("sources"))
check("research returned real sources", len(sources) >= 1, f"{len(sources)} sources; first={sources[:1]}")

verdict = fs.get("compliance_verdict") or {}
check(
    "compliance verdict is a real decision",
    isinstance(verdict.get("approved"), bool) and bool(verdict.get("risk_level")),
    f"approved={verdict.get('approved')} risk={verdict.get('risk_level')} flags={len(as_list(verdict.get('flagged_issues')))}",
)

print("== 3. review packet + pending queue")
status, packet = call("GET", f"/reviews/{cid}/packet")
pk = (packet or {}).get("packet") or {}
check("GET /reviews/{id}/packet", status == 200, f"status={status}")
check("packet carries the upstream payloads",
      all(pk.get(k) for k in ("creative", "media_plan", "compliance_verdict")),
      f"keys={sorted(pk.keys())}")

status, pending = call("GET", "/reviews/pending")
rows = (pending or {}).get("pending") or []
check("campaign appears in the pending queue",
      any(r.get("id") == cid for r in rows), f"pending={len(rows)}")

print("== 4. approve (HITL resume)")
status, approved = call("POST", f"/reviews/{cid}/approve",
                        {"approved": True, "comments": "Automated end-to-end verification run."},
                        extra={"X-AdForge-Reviewer": "reviewer@adforge.local"})
check("POST /reviews/{id}/approve", status == 200, f"status={status} {str(approved)[:300]}")
ap = approved if isinstance(approved, dict) else {}
check("single-use approval token issued", bool(ap.get("approval_token_issued")), f"next={ap.get('next_step')}")
check("reviewer recorded", ap.get("reviewer") == "reviewer@adforge.local", f"reviewer={ap.get('reviewer')}")

state_keys = ap.get("final_state_keys") or []
for key in ("kpi_analytics", "roi_calculator", "optimization"):
    check(f"graph resumed into: {key}", key in state_keys, f"keys={len(state_keys)}")

print("== 5. traces + audit")
status, runs = call("GET", f"/traces/{cid}/runs")
payload = (runs or {}).get("runs") if isinstance(runs, dict) else runs
agent_names = {r.get("agent_name") for r in (payload or [])}
check("traces recorded for the full pipeline", status == 200 and len(agent_names) >= 7,
      f"runs={len(payload or [])} agents={sorted(a for a in agent_names if a)}")
check("downstream agents ran after approval",
      {"kpi_analytics", "roi_calculator", "optimization"} <= agent_names,
      f"missing={sorted({'kpi_analytics','roi_calculator','optimization'} - agent_names)}")

status, audit = call("GET", f"/traces/{cid}/audit")
events = (audit or {}).get("events") if isinstance(audit, dict) else audit
check("append-only audit trail", status == 200 and len(events or []) > 0, f"events={len(events or [])}")

print("== 6. KPI endpoints")
status, snaps = call("GET", f"/kpis/{cid}/snapshots")
rows = (snaps or {}).get("snapshots") or []
check("GET /kpis/{id}/snapshots returns rows", status == 200 and len(rows) > 0, f"count={len(rows)}")
if rows:
    check("mock rows are labelled is_mock", all(r.get("is_mock") is True for r in rows[:5]),
          f"is_mock={[r.get('is_mock') for r in rows[:3]]}")
    check("spend is populated", sum(float(r.get("spend_usd") or 0) for r in rows) > 0,
          f"total_spend={sum(float(r.get('spend_usd') or 0) for r in rows):.2f}")

status, agg = call("GET", f"/kpis/{cid}/aggregate")
buckets = (agg or {}).get("buckets") or []
check("GET /kpis/{id}/aggregate groups by channel", status == 200 and len(buckets) > 0,
      f"buckets={[b.get('bucket') for b in buckets]}")
check("aggregate buckets carry the mock flag",
      all("is_mock" in b and "all_simulated" in b for b in buckets),
      f"flags={[(b.get('is_mock'), b.get('all_simulated')) for b in buckets]}")

print("== 7. data isolation")
status, _ = call("GET", f"/campaigns/{cid}", extra={"X-AdForge-Tenant": "other_tenant"})
check("cross-tenant read is refused", status in (403, 404), f"status={status}")

print()
if failures:
    print(f"FAILED {len(failures)} check(s): " + "; ".join(failures))
    _cleanup()
    sys.exit(1)
print("ALL CHECKS PASSED")
_cleanup()
