"""Conversation ORM model — threads between sellers and clients."""
from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, DictMixin, TimestampMixin


class Conversation(Base, TimestampMixin, DictMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        # Índices únicos parciales: ver migración 005 (sin vendedor / con vendedor)
        CheckConstraint(
            "status IN ('OPEN', 'CLOSED')",
            name="ck_conversations_status",
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    company_id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, ForeignKey("companies.id"), nullable=False
    )
    seller_user_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=True
    )
    client_id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, ForeignKey("clients.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="OPEN"
    )
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Marca temporal del cierre (staff PATCH); null si OPEN o nunca cerrada en este ciclo.
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # relationships
    company = relationship("Company", back_populates="conversations")
    seller = relationship(
        "User", back_populates="seller_conversations", foreign_keys=[seller_user_id]
    )
    client = relationship("Client", back_populates="conversations")
    messages = relationship(
        "Message", back_populates="conversation", cascade="all, delete-orphan"
    )
