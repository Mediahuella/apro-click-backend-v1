"""Agrega columnas apro_ia_id y apro_ia_folio a quote_requests.

Revision ID: 022
Revises: 021
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = "022"
down_revision = "fa4686927269"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quote_requests",
        sa.Column("apro_ia_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "quote_requests",
        sa.Column("apro_ia_folio", sa.String(20), nullable=True),
    )
    op.create_index(
        "ix_quote_requests_apro_ia_folio",
        "quote_requests",
        ["apro_ia_folio"],
    )


def downgrade() -> None:
    op.drop_index("ix_quote_requests_apro_ia_folio", table_name="quote_requests")
    op.drop_column("quote_requests", "apro_ia_folio")
    op.drop_column("quote_requests", "apro_ia_id")
