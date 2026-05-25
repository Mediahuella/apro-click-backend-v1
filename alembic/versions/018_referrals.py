"""referrals — link de invitación de usuarios SALES atribuye altas de empresas

Agrega:
- `users.referral_code` (unique, nullable): código corto opaco que cada usuario SALES
  comparte como `/pages/registro-empresas?from=<code>` para atribuirse el alta.
- `company_registration_requests.referred_by_user_id` (FK users.id, nullable): vendedor
  resuelto al recibir el `referral_code` en el POST público.
- `companies.referred_by_user_id` (FK users.id, nullable): se propaga al aprobar la
  solicitud para dejar la asignación de cartera fija en la empresa.

Backfill: genera `referral_code` para todos los usuarios `role=SALES` existentes.

Revision ID: 018
Revises: 017
"""

from __future__ import annotations

import secrets
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _generate_unique_code(conn: sa.Connection, existing: set[str]) -> str:
    """Devuelve un código de 10 chars no usado todavía (in-memory + DB unique constraint)."""
    while True:
        code = secrets.token_urlsafe(8)[:10]
        if code in existing:
            continue
        existing.add(code)
        return code


def upgrade() -> None:
    # 1) users.referral_code
    op.add_column(
        "users",
        sa.Column("referral_code", sa.String(length=32), nullable=True),
    )
    op.create_unique_constraint(
        "uq_users_referral_code", "users", ["referral_code"]
    )

    # 2) company_registration_requests.referred_by_user_id
    op.add_column(
        "company_registration_requests",
        sa.Column("referred_by_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_reg_requests_referred_by_user",
        "company_registration_requests",
        "users",
        ["referred_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_reg_requests_referred_by_user_id",
        "company_registration_requests",
        ["referred_by_user_id"],
    )

    # 3) companies.referred_by_user_id
    op.add_column(
        "companies",
        sa.Column("referred_by_user_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_companies_referred_by_user",
        "companies",
        "users",
        ["referred_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_companies_referred_by_user_id",
        "companies",
        ["referred_by_user_id"],
    )

    # 4) Backfill: códigos únicos para todos los SALES sin código.
    conn = op.get_bind()
    sales_ids = [
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT id FROM users "
                "WHERE role = 'SALES' AND referral_code IS NULL"
            )
        )
    ]
    existing: set[str] = {
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT referral_code FROM users WHERE referral_code IS NOT NULL"
            )
        )
    }
    for uid in sales_ids:
        code = _generate_unique_code(conn, existing)
        conn.execute(
            sa.text("UPDATE users SET referral_code = :c WHERE id = :id"),
            {"c": code, "id": uid},
        )


def downgrade() -> None:
    op.drop_index(
        "ix_companies_referred_by_user_id", table_name="companies"
    )
    op.drop_constraint(
        "fk_companies_referred_by_user", "companies", type_="foreignkey"
    )
    op.drop_column("companies", "referred_by_user_id")

    op.drop_index(
        "ix_reg_requests_referred_by_user_id",
        table_name="company_registration_requests",
    )
    op.drop_constraint(
        "fk_reg_requests_referred_by_user",
        "company_registration_requests",
        type_="foreignkey",
    )
    op.drop_column(
        "company_registration_requests", "referred_by_user_id"
    )

    op.drop_constraint("uq_users_referral_code", "users", type_="unique")
    op.drop_column("users", "referral_code")
