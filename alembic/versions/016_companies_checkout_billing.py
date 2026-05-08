"""companies.* — campos opcionales de facturación para checkout (note_attributes)

Revision ID: 016
Revises: 015
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: Union[str, None] = "015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column("billing_documento", sa.String(64), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("billing_rut", sa.String(32), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("billing_razon_social", sa.String(512), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("billing_giro", sa.String(256), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("billing_region", sa.String(256), nullable=True),
    )
    op.add_column(
        "companies",
        sa.Column("billing_direccion", sa.String(512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companies", "billing_direccion")
    op.drop_column("companies", "billing_region")
    op.drop_column("companies", "billing_giro")
    op.drop_column("companies", "billing_razon_social")
    op.drop_column("companies", "billing_rut")
    op.drop_column("companies", "billing_documento")
