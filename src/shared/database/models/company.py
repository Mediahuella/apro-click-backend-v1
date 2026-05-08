"""Company ORM model."""
from __future__ import annotations

import uuid as uuid_mod

from sqlalchemy import Boolean, CheckConstraint, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, DictMixin, TimestampMixin


class Company(Base, TimestampMixin, DictMixin):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(
            "company_type IN ('SMALL', 'MEDIUM', 'BIG')",
            name="ck_companies_company_type",
        ),
        CheckConstraint(
            "payment_type IN ('DIRECT', 'CREDIT')",
            name="ck_companies_payment_type",
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    company_type: Mapped[str] = mapped_column(
        String, nullable=False, default="SMALL"
    )
    payment_type: Mapped[str] = mapped_column(
        String, nullable=False, default="DIRECT"
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    shopify_company_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    billing_documento: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    billing_rut: Mapped[str | None] = mapped_column(String(32), nullable=True)
    billing_razon_social: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    billing_giro: Mapped[str | None] = mapped_column(String(256), nullable=True)
    billing_region: Mapped[str | None] = mapped_column(String(256), nullable=True)
    billing_direccion: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )

    # relationships
    users = relationship("User", back_populates="company")
    clients = relationship("Client", back_populates="company")
    conversations = relationship("Conversation", back_populates="company")
    shopify_installations = relationship(
        "ShopifyAppInstallation", back_populates="company"
    )
    shopify_orders = relationship("ShopifyOrder", back_populates="company")
    user_company_links = relationship(
        "UserCompany", back_populates="company", cascade="all, delete-orphan"
    )
