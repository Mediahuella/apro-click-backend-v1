"""User ORM model — linked to Cognito via cognito_sub."""
from __future__ import annotations

import uuid as uuid_mod

from sqlalchemy import CheckConstraint, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, DictMixin, TimestampMixin

VALID_ROLES = frozenset({"SUPERADMIN", "ADMIN", "SALES", "KPI_VISUALIZERS"})
VALID_STATUSES = frozenset({"ACTIVE", "DISABLED", "PENDING"})

ROLE_TO_COGNITO_GROUP: dict[str, str] = {
    "SUPERADMIN": "superadmin",
    "ADMIN": "admin",
    "SALES": "sales",
    "KPI_VISUALIZERS": "kpi_visualizers",
}

_LEGACY_ROLE: dict[str, str] = {
    "superadmin": "SUPERADMIN",
    "admin": "ADMIN",
    "sales": "SALES",
    "kpi_visualizers": "KPI_VISUALIZERS",
}

_LEGACY_STATUS: dict[str, str] = {
    "active": "ACTIVE",
    "inactive": "DISABLED",
    "pending": "PENDING",
}


def coerce_role(role: str) -> str:
    r = role.strip()
    if r in _LEGACY_ROLE:
        return _LEGACY_ROLE[r]
    u = r.upper()
    return u if u in VALID_ROLES else r


def coerce_status(status: str) -> str:
    s = status.strip()
    if s in _LEGACY_STATUS:
        return _LEGACY_STATUS[s]
    u = s.upper()
    return u if u in VALID_STATUSES else s


class User(Base, TimestampMixin, DictMixin):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "role IN ('SUPERADMIN', 'ADMIN', 'SALES', 'KPI_VISUALIZERS')",
            name="ck_users_role",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED', 'PENDING')",
            name="ck_users_status",
        ),
    )

    id: Mapped[uuid_mod.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid_mod.uuid4
    )
    cognito_sub: Mapped[str] = mapped_column(
        String, unique=True, nullable=False
    )
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    given_name: Mapped[str] = mapped_column(String, default="")
    family_name: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String, nullable=False, default="SALES")
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="PENDING"
    )
    company_id: Mapped[uuid_mod.UUID | None] = mapped_column(
        Uuid, ForeignKey("companies.id"), nullable=True
    )
    #: GID GraphQL `gid://shopify/StaffMember/...` cuando se vinculó con la tienda.
    shopify_staff_member_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )
    #: LINKED | NOT_FOUND | SKIPPED_ROLE | SKIPPED_NO_SHOP | ERROR (ver utils shopify staff).
    shopify_staff_link_status: Mapped[str | None] = mapped_column(
        String, nullable=True
    )

    # relationships
    company = relationship("Company", back_populates="users")
    audit_logs = relationship("AuditLog", back_populates="actor")
    seller_conversations = relationship(
        "Conversation", back_populates="seller", foreign_keys="Conversation.seller_user_id"
    )
    shopify_order_interventions = relationship(
        "ShopifyOrder",
        back_populates="last_intervened_by_user",
        foreign_keys="ShopifyOrder.last_intervened_by_user_id",
    )
    user_company_links = relationship(
        "UserCompany", back_populates="user", cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["sub"] = d["cognito_sub"]
        return d
