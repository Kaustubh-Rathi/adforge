"""DB seed — populates 3 example campaigns with sample KPI snapshots.

Run via:  python -m adforge.storage.seed
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from adforge.storage.session import init_engine, dispose_engine, get_session_factory

EXAMPLES_FILE = Path(__file__).parent.parent / "examples" / "campaigns.json"


async def seed() -> None:
    init_engine()
    factory = get_session_factory()
    examples = json.loads(EXAMPLES_FILE.read_text())

    async with factory() as session:
        for ex in examples:
            campaign_id = uuid4()
            tenant_id = ex["tenant_id"]
            await session.execute(
                text(
                    "INSERT INTO campaigns (id, tenant_id, name, brief, status, started_at) "
                    "VALUES (:id, :tid, :name, :brief::jsonb, :status, :started)"
                ),
                {
                    "id": str(campaign_id),
                    "tid": tenant_id,
                    "name": ex["name"],
                    "brief": json.dumps(ex["brief"]),
                    "status": "live",
                    "started": datetime.now(timezone.utc) - timedelta(days=ex["days_live"]),
                },
            )

            # Seed KPI snapshots — one per day per channel.
            for channel in ex["brief"].get("channels_hint", ["google", "meta"]):
                for day_offset in range(ex["days_live"]):
                    impressions = random.randint(2000, 12000)
                    clicks = int(impressions * random.uniform(0.01, 0.05))
                    spend = round(clicks * random.uniform(0.8, 2.5), 2)
                    conversions = int(clicks * random.uniform(0.03, 0.10))
                    revenue = round(conversions * ex["avg_conversion_value"], 2)
                    captured = datetime.now(timezone.utc) - timedelta(days=day_offset)
                    await session.execute(
                        text(
                            "INSERT INTO kpi_snapshots "
                            "(tenant_id, campaign_id, channel, impressions, clicks, spend_usd, "
                            " conversions, revenue_usd, ctr, cvr, cpc, cpa, cpm, roas, captured_at) "
                            "VALUES (:tid, :cid, :ch, :imp, :clk, :spd, :cnv, :rev, "
                            "        :ctr, :cvr, :cpc, :cpa, :cpm, :roas, :cap)"
                        ),
                        {
                            "tid": tenant_id, "cid": str(campaign_id), "ch": channel,
                            "imp": impressions, "clk": clicks, "spd": spend,
                            "cnv": conversions, "rev": revenue,
                            "ctr": round(clicks / impressions, 4),
                            "cvr": round(conversions / clicks, 4) if clicks else 0.0,
                            "cpc": round(spend / clicks, 2) if clicks else 0.0,
                            "cpa": round(spend / conversions, 2) if conversions else 0.0,
                            "cpm": round(spend / impressions * 1000, 2),
                            "roas": round(revenue / spend, 2) if spend else 0.0,
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

    await dispose_engine()
    print(f"Seeded {len(examples)} example campaigns with KPI snapshots.")


def run() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    run()
