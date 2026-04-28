"""M2M: usuario staff ↔ empresas cuyas entidades (pedidos, etc.) puede ver."""
from __future__ import annotations

import uuid as uuid_mod

from sqlalchemy import ForeignKey, PrimaryKeyConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base


class UserCompany(Base):
    __tablename__ = "user_companies"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "company_id", name="pk_user_companies"),
    )

    user_id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )

    user = relationship("User", back_populates="user_company_links")
    company = relationship("Company", back_populates="user_company_links")
