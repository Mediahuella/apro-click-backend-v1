"""AuditLog ORM model — traces actions performed by system users."""
from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, DictMixin


class AuditLog(Base, DictMixin):
    __tablename__ = "audit_logs"

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    actor_user_id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=False
    )
    metadata_: Mapped[dict | None] = mapped_column(
        "metadata", JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # relationships
    actor = relationship("User", back_populates="audit_logs")
