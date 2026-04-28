"""CompanyRegistrationRequest ORM model — form submissions for new companies."""
from __future__ import annotations

import uuid as uuid_mod

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, DictMixin, TimestampMixin


class CompanyRegistrationRequest(Base, TimestampMixin, DictMixin):
    __tablename__ = "company_registration_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'NEEDS_INFO')",
            name="ck_reg_requests_status",
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDING"
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    submitted_email: Mapped[str | None] = mapped_column(String, nullable=True)
    resolved_company_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid, ForeignKey("companies.id"), nullable=True
    )
    resolved_by_user_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # relationships
    resolved_company = relationship("Company")
    resolved_by = relationship("User")
