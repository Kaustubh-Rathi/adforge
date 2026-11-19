"""DB seed — populates 3 example campaigns with sample KPI snapshots.

Run via:  python -m adforge.storage.seed

Re-running is safe and idempotent: each example campaign is addressed by a
deterministic id derived from its tenant + name, and its previous KPI rows are
cleared first. Without that, every `make seed` would append another copy of
each campaign and quietly double the demo's figures.
"""

from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text

from adforge.config import get_settings
from adforge.storage import session as session_module

EXAMPLES_FILE = Path(__file__).parent.parent / "examples" / "campaigns.json"


def _campaign_id(tenant_id: str, name: str) -> str:
    """Stable id per example, so re-seeding rewrites instead of duplicating."""
    return str(uuid5(NAMESPACE_URL, f"adforge:seed:{tenant_id}:{name}"))


async def seed() -> None:
    settings = get_settings()
    if settings.app_env not in ("dev", "test"):
        raise RuntimeError(
            "Refusing to seed sample campaigns outside dev/test: "
            f"APP_ENV={settings.app_env!r}. Seeded rows are fabricated and must "
            "never be presented as real performance data."
        )
    session_module.init_engine()
    factory = session_module.get_session_factory()
    examples = json.loads(EXAMPLES_FILE.read_text())

    async with factory() as session:
        for ex in examples:
            tenant_id = ex["tenant_id"]
            campaign_id = _campaign_id(tenant_id, ex["name"])

            # Clear the previous KPI rows for this exact campaign so the demo
            # figures are regenerated, never compounded across runs.
            await session.execute(
                text("DELETE FROM kpi_snapshots WHERE campaign_id = :cid"),
                {"cid": campaign_id},
            )
            await session.execute(
                text(
                    "INSERT INTO campaigns (id, tenant_id, name, brief, status, started_at) "
                    "VALUES (:id, :tid, :name, CAST(:brief AS JSONB), :status, :started) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    " tenant_id = EXCLUDED.tenant_id, name = EXCLUDED.name, "
                    " brief = EXCLUDED.brief, status = EXCLUDED.status, "
                    " started_at = EXCLUDED.started_at, updated_at = now()"
                ),
                {
                    "id": campaign_id,
                    "tid": tenant_id,
                    "name": ex["name"],
                    "brief": json.dumps(ex["brief"]),
                    "status": "live",
                    "started": datetime.now(UTC) - timedelta(days=ex["days_live"]),
                },
            )

            # Seed KPI snapshots — one per day per channel. A per-campaign RNG
            # keeps re-seeding reproducible instead of reshuffling the numbers.
            rng = random.Random(f"adforge:seed:{ex['tenant_id']}:{ex['name']}")
            for channel in ex["brief"].get("channels_hint", ["google", "meta"]):
                for day_offset in range(ex["days_live"]):
                    impressions = rng.randint(2000, 12000)
                    clicks = int(impressions * rng.uniform(0.01, 0.05))
                    spend = round(clicks * rng.uniform(0.8, 2.5), 2)
                    conversions = int(clicks * rng.uniform(0.03, 0.10))
                    revenue = round(conversions * ex["avg_conversion_value"], 2)
                    captured = datetime.now(UTC) - timedelta(days=day_offset)
                    await session.execute(
                        text(
                            "INSERT INTO kpi_snapshots "
                            "(tenant_id, campaign_id, channel, impressions, clicks, spend_usd, "
                            " conversions, revenue_usd, ctr, cvr, cpc, cpa, cpm, roas, "
                            " source, is_mock, platform_date, captured_at) "
                            "VALUES (:tid, :cid, :ch, :imp, :clk, :spd, :cnv, :rev, "
                            "        :ctr, :cvr, :cpc, :cpa, :cpm, :roas, "
                            "        'seed', TRUE, :pdate, :cap)"
                        ),
                        {
                            "tid": tenant_id,
                            "cid": campaign_id,
                            "ch": channel,
                            "imp": impressions,
                            "clk": clicks,
                            "spd": spend,
                            "cnv": conversions,
                            "rev": revenue,
                            "ctr": round(clicks / impressions, 4),
                            "cvr": round(conversions / clicks, 4) if clicks else 0.0,
                            "cpc": round(spend / clicks, 2) if clicks else 0.0,
                            "cpa": round(spend / conversions, 2) if conversions else 0.0,
                            "cpm": round(spend / impressions * 1000, 2),
                            "roas": round(revenue / spend, 2) if spend else 0.0,
                            "pdate": captured.date(),
                            "cap": captured,
                        },
                    )

            # Update totals
            await session.execute(
                text(
                    "UPDATE campaigns SET total_spend_usd = ("
                    "   SELECT COALESCE(SUM(spend_usd), 0) FROM kpi_snapshots WHERE campaign_id = :cid"
                    "), total_revenue_usd = ("
                    "   SELECT COALESCE(SUM(revenue_usd), 0) FROM kpi_snapshots WHERE campaign_id = :cid"
                    ") WHERE id = :cid"
                ),
                {"cid": str(campaign_id)},
            )

        await session.commit()

    await session_module.dispose_engine()
    print(f"Seeded {len(examples)} example campaigns with KPI snapshots.")


def run() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    run()
