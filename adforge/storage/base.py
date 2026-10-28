"""SQLAlchemy declarative base + common mixins."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID as PgUUID, JSONB


class Base(DeclarativeBase):
    """Declarative base for all AdForge ORM models."""

    type_annotation_map = {dict[str, Any]: JSONB}


class TenantMixin:
    """Mixin enforcing tenant_id on every row.

    Combined with the storage-layer session listener, this guarantees
    every SELECT carries a tenant filter and every INSERT carries the
    caller's tenant_id. See ``guardrails.data_isolation``.
    """

    tenant_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)


class TimestampMixin:
    """created_at + updated_at columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
