"""Dirección/sucursal de una empresa B2B (1:N con companies, espejo de Shopify CompanyLocation)."""
from __future__ import annotations

import uuid as uuid_mod

from sqlalchemy import Boolean, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, DictMixin, TimestampMixin


class CompanyAddress(Base, TimestampMixin, DictMixin):
    __tablename__ = "company_addresses"

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    company_id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid,
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    shopify_location_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )

    address1: Mapped[str] = mapped_column(String(512), nullable=False)
    address2: Mapped[str | None] = mapped_column(String(512), nullable=True)
    city: Mapped[str] = mapped_column(String(256), nullable=False)
    zone_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    country_code: Mapped[str] = mapped_column(
        String(2), nullable=False, default="CL", server_default="CL"
    )
    zip: Mapped[str | None] = mapped_column(String(32), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    company = relationship("Company", back_populates="addresses")
