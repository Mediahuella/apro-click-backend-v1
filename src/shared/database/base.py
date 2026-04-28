"""SQLAlchemy declarative base and shared mixins."""
from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, func, inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class TimestampMixin:
    """Adds created_at / updated_at columns managed by the DB."""

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


class DictMixin:
    """Generic to_dict() for any mapped model."""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for attr in sa_inspect(self.__class__).mapper.column_attrs:
            val = getattr(self, attr.key)
            if isinstance(val, uuid_mod.UUID):
                val = str(val)
            elif isinstance(val, datetime):
                val = val.isoformat()
            result[attr.key] = val
        return result
