"""agregar shipping_line a quote_requests

Revision ID: 021
Revises: 020_quote_requests
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quote_requests",
        sa.Column("cart_token", sa.String(64), nullable=True),
    )
    op.create_index("ix_quote_requests_cart_token", "quote_requests", ["cart_token"])
    op.add_column(
        "quote_requests",
        sa.Column("shipping_line", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("quote_requests", "shipping_line")
    op.drop_index("ix_quote_requests_cart_token", table_name="quote_requests")
    op.drop_column("quote_requests", "cart_token")
