"""Async Redis client (singleton). Used by:
  - guardrails.rate_limiter  (token buckets)
  - langgraph-checkpoint-redis (agent state persistence)
  - guardrails.data_isolation (cached tenant contexts)
"""
from __future__ import annotations

from typing import Optional

import redis.asyncio as redis

from adforge.config import get_settings

_redis: Optional[redis.Redis] = None


async def get_redis() -> redis.Redis:
    """Singleton async Redis client."""
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis


async def get_redis_async() -> redis.Redis:
    """Alias for code that prefers the explicit async name."""
    return await get_redis()


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None
