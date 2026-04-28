"""Client ORM model — external contacts linked to a company + Shopify."""
from __future__ import annotations

import uuid as uuid_mod

from sqlalchemy import ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, DictMixin, TimestampMixin


class Client(Base, TimestampMixin, DictMixin):
    __tablename__ = "clients"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "shopify_customer_id",
            name="uq_clients_company_shopify",
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    company_id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, ForeignKey("companies.id"), nullable=False
    )
    shopify_customer_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)

    # relationships
    company = relationship("Company", back_populates="clients")
    conversations = relationship("Conversation", back_populates="client")
