"""Token-bucket rate limiter backed by Redis.

Every LLM call and every ad-platform API call MUST pass through this limiter.
Two scopes:
    - "llm:<campaign_id>"  — bounded by guardrails_max_llm_calls_per_campaign
    - "<platform>:<tenant_id>" — bounded by guardrails_max_api_calls_per_min

The limiter is per-tenant + per-campaign — one runaway campaign cannot starve
another. Backed by Redis INCR with TTL so it works across multiple API workers.
"""

from __future__ import annotations

import time
from typing import Literal, Optional

from adforge.guardrails.exceptions import RateLimitExceeded
from adforge.storage.redis_client import get_redis


Scope = Literal["llm", "google", "meta", "linkedin", "tiktok"]


class RateLimiter:
    """Distributed token-bucket limiter. No-op when guardrails inactive."""

    def __init__(self, max_per_minute: int, max_per_campaign: Optional[int] = None) -> None:
        self.max_per_minute = max_per_minute
        self.max_per_campaign = max_per_campaign

    async def acquire(self, scope: Scope, tenant_id: str, campaign_id: str) -> None:
        """Block (or raise) until a slot is available.

        Raises ``RateLimitExceeded`` if the per-campaign budget is exhausted —
        we don't wait, we fail fast so the graph can route around the breach.
        """
        from adforge.config import get_settings

        settings = get_settings()
        if not settings.guardrails_active:
            return  # noop in test mode with guardrails disabled

        redis = await get_redis()
        now = int(time.time())
        minute_key = f"ratelimit:{scope}:{tenant_id}:{now // 60}"
        count = await redis.incr(minute_key)
        if count == 1:
            await redis.expire(minute_key, 70)  # 60s + 10s grace

        if count > self.max_per_minute:
            raise RateLimitExceeded(
                scope=f"{scope}:{tenant_id}",
                limit=self.max_per_minute,
                window_seconds=60,
            )

        if self.max_per_campaign is not None and scope == "llm":
            campaign_key = f"ratelimit:llm:campaign:{campaign_id}"
            campaign_count = await redis.incr(campaign_key)
            if campaign_count == 1:
                await redis.expire(campaign_key, 86400)  # 24h TTL

            if campaign_count > self.max_per_campaign:
                raise RateLimitExceeded(
                    scope=f"llm:campaign:{campaign_id}",
                    limit=self.max_per_campaign,
                    window_seconds=86400,
                )


# Module-level singletons, constructed lazily through get_rate_limiter() so
# tests can monkeypatch settings without re-creating.
_limiters: dict[str, RateLimiter] = {}


def get_rate_limiter(scope: Scope) -> RateLimiter:
    """Return the rate limiter configured for a given scope.

    Different scopes have different limits — see Settings for the source.
    """
    if scope in _limiters:
        return _limiters[scope]

    from adforge.config import get_settings

    s = get_settings()
    if scope == "llm":
        lim = RateLimiter(
            max_per_minute=s.guardrails_max_api_calls_per_min,
            max_per_campaign=s.guardrails_max_llm_calls_per_campaign,
        )
    else:
        lim = RateLimiter(max_per_minute=s.guardrails_max_api_calls_per_min)
    _limiters[scope] = lim
    return lim
