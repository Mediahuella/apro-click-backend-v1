"""Comentario por línea de cotización.

SALES anota observaciones por fila (plazo de entrega, marca alternativa, lo que
pidió el cliente). Solo se usa en el panel; no viaja al Draft Order de Shopify.

Revision ID: 031
Revises: 030
Create Date: 2026-10-07
"""
import sqlalchemy as sa
from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quote_request_lines",
        sa.Column("comment", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("quote_request_lines", "comment")
