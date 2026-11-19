"""Clear the demo tenant so a recording starts from a known-empty state.

Deletes the tenant's campaigns and every row that references them, plus the
LangGraph checkpoints for each campaign. Postgres and Redis must be running.

Usage:
    python scripts/demo_video/reset_demo_tenant.py [tenant_id]
"""

from __future__ import annotations

import asyncio
import sys

import asyncpg

from adforge.config import get_settings
from adforge.graph.state import clear_thread

# Children first; the FK constraints are not cascading.
_CHILD_TABLES = ("agent_runs", "kpi_snapshots", "creative_artifacts", "audit_events")


def asyncpg_dsn() -> str:
    """The configured async URL with SQLAlchemy's driver prefix stripped."""
    url = get_settings().database_url
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def main(tenant: str) -> None:
    conn = await asyncpg.connect(asyncpg_dsn())
    try:
        ids = [
            r["id"]
            for r in await conn.fetch("select id from campaigns where tenant_id = $1", tenant)
        ]
        print(f"{tenant}: {len(ids)} campaign(s)")

        for campaign_id in ids:
            await clear_thread(campaign_id)

        for table in _CHILD_TABLES:
            deleted = await conn.execute(
                f"delete from {table} where tenant_id = $1",  # noqa: S608 - fixed table names
                tenant,
            )
            print(f"  {table}: {deleted}")

        deleted = await conn.execute("delete from campaigns where tenant_id = $1", tenant)
        print(f"  campaigns: {deleted}")
    finally:
        await conn.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "demo_tenant"
    asyncio.run(main(target))
