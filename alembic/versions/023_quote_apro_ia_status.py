"""Agrega columna apro_ia_status a quote_requests (estado del webhook APRO IA).

Revision ID: 023
Revises: 022
Create Date: 2026-07-06
"""
from alembic import op
import sqlalchemy as sa

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quote_requests",
        sa.Column("apro_ia_status", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("quote_requests", "apro_ia_status")
