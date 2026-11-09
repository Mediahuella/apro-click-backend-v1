"""Fecha y sucursal de retiro en tienda en shopify_orders.

La extensión de checkout ``fecha-retiro`` (aproclick-connector) guarda la fecha
que elige el cliente como atributos del pedido (``fecha_retiro``,
``sucursal_retiro``); el webhook de ``orders`` los persiste aquí para que el
panel pueda listar y filtrar los retiros por día.

Revision ID: 032
Revises: 031
Create Date: 2026-10-07
"""
from alembic import op
import sqlalchemy as sa

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("shopify_orders", sa.Column("pickup_date", sa.Date(), nullable=True))
    op.add_column(
        "shopify_orders", sa.Column("pickup_location", sa.String(255), nullable=True)
    )
    op.create_index(
        "ix_shopify_orders_pickup_date", "shopify_orders", ["pickup_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_shopify_orders_pickup_date", table_name="shopify_orders")
    op.drop_column("shopify_orders", "pickup_location")
    op.drop_column("shopify_orders", "pickup_date")
