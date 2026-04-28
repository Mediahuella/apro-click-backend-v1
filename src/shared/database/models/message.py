"""Message ORM model — individual messages within a conversation."""
from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, DictMixin


class Message(Base, DictMixin):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "sender_type IN ('USER', 'CLIENT')",
            name="ck_messages_sender_type",
        ),
        CheckConstraint(
            "(sender_type = 'USER' AND sender_user_id IS NOT NULL AND sender_client_id IS NULL) OR "
            "(sender_type = 'CLIENT' AND sender_client_id IS NOT NULL AND sender_user_id IS NULL)",
            name="ck_messages_sender_consistency",
        ),
        CheckConstraint(
            "message_type IN ('TEXT', 'IMAGE', 'FILE')",
            name="ck_messages_type",
        ),
        CheckConstraint(
            "(body IS NOT NULL) OR (attachment_key IS NOT NULL)",
            name="ck_messages_body_or_attachment",
        ),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    conversation_id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sender_type: Mapped[str] = mapped_column(String, nullable=False)
    sender_user_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=True
    )
    sender_client_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid, ForeignKey("clients.id"), nullable=True
    )
    message_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="TEXT"
    )
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # S3 key (no URL completa — se genera presigned en runtime)
    attachment_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation = relationship("Conversation", back_populates="messages")
    sender_user = relationship("User", foreign_keys=[sender_user_id])
    sender_client = relationship("Client", foreign_keys=[sender_client_id])
