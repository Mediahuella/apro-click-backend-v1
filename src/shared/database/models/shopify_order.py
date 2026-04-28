"""Shopify order snapshot — synced via webhooks; intervención en PENDING."""
from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    Uuid,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from database.base import Base, DictMixin, TimestampMixin


class _ShopifyOrderIdAsText(TypeDecorator[str]):
    """Shopify envía `id` numérico en JSON; en BD debe persistirse como texto."""

    impl = String(128)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            raise ValueError("shopify_order_id no puede estar vacío")
        return s

    def process_result_value(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return str(value)


class ShopifyOrder(Base, TimestampMixin, DictMixin):
    __tablename__ = "shopify_orders"
    __table_args__ = (
        CheckConstraint(
            "internal_status IN ('PENDING', 'CLOSED')",
            name="ck_shopify_orders_internal_status",
        ),
        UniqueConstraint(
            "shop_domain",
            "shopify_order_id",
            name="uq_shopify_orders_shop_order",
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    shopify_order_id: Mapped[str] = mapped_column(
        _ShopifyOrderIdAsText(), nullable=False, index=True
    )
    shop_domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    company_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid, ForeignKey("companies.id"), nullable=True, index=True
    )
    order_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    financial_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fulfillment_status: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    subtotal_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    total_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    internal_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING"
    )
    shopify_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    intervention_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_intervened_by_user_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=True
    )
    line_items: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
        doc="Líneas del pedido (título, cantidad, SKU, precios) para listados/UI.",
    )

    company = relationship("Company", back_populates="shopify_orders")
    last_intervened_by_user = relationship(
        "User", foreign_keys=[last_intervened_by_user_id]
    )

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        for k, v in list(data.items()):
            if isinstance(v, Decimal):
                data[k] = str(v) if v is not None else None
        soid = data.get("shopify_order_id")
        if soid is not None:
            data["shopify_order_id"] = str(soid)
        raw_lines = data.get("line_items")
        if raw_lines is None:
            data["line_items"] = []
        elif isinstance(raw_lines, list):
            normalized: list[dict[str, Any]] = []
            for li in raw_lines:
                if not isinstance(li, dict):
                    continue
                item = dict(li)
                for key in ("id", "product_id", "variant_id"):
                    if item.get(key) is not None:
                        item[key] = str(item[key])
                normalized.append(item)
            data["line_items"] = normalized
        return data
