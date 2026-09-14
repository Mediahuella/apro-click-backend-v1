"""Medio de pago del pedido en shopify_orders.

Persiste ``payment_gateway_names`` del payload de Shopify (ej. ``["Bank
Deposit"]`` para transferencia, ``["Transbank Webpay"]``, ``["Mercado Pago
Checkout Pro"]``) para poder distinguir en el panel los pedidos pagados por
transferencia, que son los que cierra el webhook de APRO IA.

Revision ID: 027
Revises: 026
Create Date: 2026-08-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "shopify_orders",
        sa.Column("payment_gateway_names", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shopify_orders", "payment_gateway_names")
