"""shopify_orders — pedidos sincronizados vía webhooks

Revision ID: 007
Revises: 006
Create Date: 2026-04-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shopify_orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shopify_order_id", sa.BigInteger(), nullable=False),
        sa.Column("shop_domain", sa.String(length=255), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("order_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("financial_status", sa.String(length=64), nullable=True),
        sa.Column("fulfillment_status", sa.String(length=64), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column("subtotal_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("total_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("internal_status", sa.String(length=16), nullable=False, server_default="PENDING"),
        sa.Column("shopify_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("intervention_notes", sa.Text(), nullable=True),
        sa.Column("last_intervened_by_user_id", sa.Uuid(), nullable=True),
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
        sa.CheckConstraint(
            "internal_status IN ('PENDING', 'CLOSED')",
            name="ck_shopify_orders_internal_status",
        ),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(
            ["last_intervened_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shop_domain", "shopify_order_id", name="uq_shopify_orders_shop_order"),
    )
    op.create_index("ix_shopify_orders_shopify_order_id", "shopify_orders", ["shopify_order_id"], unique=False)
    op.create_index("ix_shopify_orders_shop_domain", "shopify_orders", ["shop_domain"], unique=False)
    op.create_index("ix_shopify_orders_company_id", "shopify_orders", ["company_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_shopify_orders_company_id", table_name="shopify_orders")
    op.drop_index("ix_shopify_orders_shop_domain", table_name="shopify_orders")
    op.drop_index("ix_shopify_orders_shopify_order_id", table_name="shopify_orders")
    op.drop_table("shopify_orders")
