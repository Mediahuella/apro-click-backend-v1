"""add_shipping_lines_to_shopify_orders

Revision ID: fa4686927269
Revises: 021
Create Date: 2026-06-16 13:45:10.876229
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'fa4686927269'
down_revision: Union[str, None] = '021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'shopify_orders',
        sa.Column('shipping_lines', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('shopify_orders', 'shipping_lines')
