"""company_addresses — sucursales/direcciones de empresas (espejo de Shopify CompanyLocation)

Revision ID: 017
Revises: 54fd1b83794a
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "017"
down_revision: Union[str, None] = "54fd1b83794a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "company_addresses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=256), nullable=False),
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("shopify_location_id", sa.String(length=128), nullable=True),
        sa.Column("address1", sa.String(length=512), nullable=False),
        sa.Column("address2", sa.String(length=512), nullable=True),
        sa.Column("city", sa.String(length=256), nullable=False),
        sa.Column("zone_code", sa.String(length=16), nullable=True),
        sa.Column(
            "country_code",
            sa.String(length=2),
            nullable=False,
            server_default="CL",
        ),
        sa.Column("zip", sa.String(length=32), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=128), nullable=True),
        sa.Column("last_name", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_company_addresses_company_id",
        "company_addresses",
        ["company_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_company_addresses_company_id", table_name="company_addresses"
    )
    op.drop_table("company_addresses")
