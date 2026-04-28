"""user_companies: N:N usuarios ↔ empresas (alcance de pedidos / chat)

Revision ID: 011
Revises: 010
Create Date: 2026-04-27
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: Union[str, None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_companies",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_user_companies_company_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_user_companies_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "company_id", name="pk_user_companies"),
    )
    op.create_index(
        "ix_user_companies_company_id", "user_companies", ["company_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_user_companies_company_id", table_name="user_companies")
    op.drop_table("user_companies")
