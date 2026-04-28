"""shopify_orders: shopify_order_id texto + line_items JSONB

Revision ID: 008
Revises: 007
Create Date: 2026-04-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "008"
down_revision: Union[str, None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    dtype = conn.execute(
        sa.text(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'shopify_orders'
              AND column_name = 'shopify_order_id'
            """
        )
    ).scalar()
    if dtype == "bigint":
        op.drop_constraint(
            "uq_shopify_orders_shop_order", "shopify_orders", type_="unique"
        )
        op.execute(
            sa.text(
                "ALTER TABLE shopify_orders ALTER COLUMN shopify_order_id "
                "TYPE VARCHAR(128) USING shopify_order_id::text"
            )
        )
        op.create_unique_constraint(
            "uq_shopify_orders_shop_order",
            "shopify_orders",
            ["shop_domain", "shopify_order_id"],
        )
    has_line_items = conn.execute(
        sa.text(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'shopify_orders'
              AND column_name = 'line_items'
            """
        )
    ).scalar()
    if not has_line_items:
        op.add_column(
            "shopify_orders",
            sa.Column("line_items", JSONB(), nullable=True),
        )


def downgrade() -> None:
    op.drop_constraint(
        "uq_shopify_orders_shop_order", "shopify_orders", type_="unique"
    )
    op.drop_column("shopify_orders", "line_items")
    op.execute(
        sa.text(
            "ALTER TABLE shopify_orders ALTER COLUMN shopify_order_id "
            "TYPE BIGINT USING shopify_order_id::bigint"
        )
    )
    op.create_unique_constraint(
        "uq_shopify_orders_shop_order",
        "shopify_orders",
        ["shop_domain", "shopify_order_id"],
    )
