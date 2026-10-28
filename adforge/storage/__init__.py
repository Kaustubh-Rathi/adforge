"""AdForge storage layer — Postgres + Redis persistence.

Public API:
    get_session_factory()  — async SQLAlchemy session factory (singleton)
    get_redis()            — async Redis client (singleton)
    Base                   — declarative base for ORM models
    Campaign, AgentRun, KpiSnapshotRow, AuditEventRow — ORM models
"""

from adforge.storage.base import Base
from adforge.storage.models import (
    Campaign,
    AgentRun,
    KpiSnapshotRow,
    AuditEventRow,
    CreativeArtifact,
)
from adforge.storage.session import (
    get_session_factory,
    init_engine,
    dispose_engine,
)
from adforge.storage.redis_client import get_redis, get_redis_async

__all__ = [
    "Base",
    "Campaign",
    "AgentRun",
    "KpiSnapshotRow",
    "AuditEventRow",
    "CreativeArtifact",
    "get_session_factory",
    "init_engine",
    "dispose_engine",
    "get_redis",
    "get_redis_async",
]
