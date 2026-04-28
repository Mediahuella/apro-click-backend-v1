"""ShopifyAppInstallation ORM model — singleton for the main Apro Click store."""
from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, DictMixin, TimestampMixin


class ShopifyAppInstallation(Base, TimestampMixin, DictMixin):
    __tablename__ = "shopify_app_installations"

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    shop_domain: Mapped[str] = mapped_column(
        String, unique=True, nullable=False
    )
    access_token_secret_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    shopify_access_token: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Admin API access token (prefer column vs Secrets Manager for simple access).",
    )
    scopes: Mapped[str | None] = mapped_column(String, nullable=True)
    installed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    uninstalled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    company_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid, ForeignKey("companies.id"), nullable=True
    )

    company = relationship("Company", back_populates="shopify_installations")

    def to_dict(self) -> dict[str, Any]:
        """Never expose Shopify tokens or legacy secret ARNs in API payloads."""
        data = super().to_dict()
        data.pop("shopify_access_token", None)
        data.pop("access_token_secret_id", None)
        return data
