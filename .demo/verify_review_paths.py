"""Exercise the two review paths the main verifier skips: reject and publish.

- reject must route the graph back to creative_strategist for a revision
- publish must refuse without a configured adapter, and must not silently
  claim a campaign went live

Run against a live API on :8000, after .demo/verify_e2e.py.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
TENANT = "verify_paths_tenant"
HDR = {"X-AdForge-Tenant": TENANT, "Content-Type": "application/json"}


def call(method, path, body=None, timeout=900, extra=None):
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
            return e.code, raw[:1200]


BRIEF = {
    "product_name": "Path Test",
    "product_description": "A product used to exercise the review paths end to end.",
    "target_audience": "Marketing leads at mid-market B2B software companies.",
    "budget_usd": 4000.0,
    "duration_days": 7,
    "channels_hint": ["google"],
    "business_objective": "awareness",
    "constraints": ["No competitor bidding."],
}

print("== create + run")
st, c = call("POST", "/campaigns", {"name": "Reject path", "brief": BRIEF})
print("create:", st)
cid = c["id"]
st, run = call("POST", f"/campaigns/{cid}/run", {})
print("run:", st, "status:", run.get("status"), "paused:", run.get("paused_for_review"))

print("\n== reject routes back for revision")
st, rej = call(
    "POST",
    f"/reviews/{cid}/reject",
    {"approved": False, "comments": "Headline is generic. Lead with the warehouse-native angle."},
    extra={"X-AdForge-Reviewer": "reviewer@adforge.local"},
)
print("reject:", st)
print(json.dumps(rej, indent=1)[:900] if isinstance(rej, dict) else rej)

st, after = call("GET", f"/campaigns/{cid}")
print("status after reject:", after.get("status") if isinstance(after, dict) else after)

print("\n== second campaign for the publish path")
st, c2 = call("POST", "/campaigns", {"name": "Publish path", "brief": BRIEF})
cid2 = c2["id"]
st, run2 = call("POST", f"/campaigns/{cid2}/run", {})
print("run:", st, "paused:", run2.get("paused_for_review"))
st, ap = call(
    "POST",
    f"/reviews/{cid2}/approve",
    {"approved": True, "comments": "ok"},
    extra={"X-AdForge-Reviewer": "reviewer@adforge.local"},
)
print("approve:", st, "token_issued:", ap.get("approval_token_issued") if isinstance(ap, dict) else ap)

print("\n== publish without a configured adapter must refuse, not lie")
st, pub = call(
    "POST", f"/reviews/{cid2}/publish", extra={"X-AdForge-Reviewer": "reviewer@adforge.local"}
)
print("publish:", st)
print(json.dumps(pub, indent=1)[:900] if isinstance(pub, dict) else str(pub)[:900])
st, final = call("GET", f"/campaigns/{cid2}")
print("status after publish attempt:", final.get("status") if isinstance(final, dict) else final)
print("\nDONE")


def _cleanup() -> None:
    """Remove this script's throwaway campaigns (best effort)."""
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

        print(f"(cleaned up {asyncio.run(_run())} review-path campaign(s))")
    except Exception as exc:
        print(f"(cleanup skipped: {exc})")


_cleanup()
