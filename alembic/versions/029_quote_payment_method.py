"""Medio de pago confirmado por APRO IA sobre las columnas ``transfer_*``.

El webhook de pagos con **crédito** reusa ``transfer_folio``/``transfer_status``/
``transfer_paid_at`` porque el ciclo es idéntico al de transferencias (confirmar
o anular un pedido que nació impago del checkout). ``payment_method`` es lo
único que permite distinguirlos después.

Las filas previas quedan en ``NULL``: por definición son transferencias, que
era el único medio que escribía esas columnas.

Revision ID: 029
Revises: 028
Create Date: 2026-09-11
"""
import sqlalchemy as sa
from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quote_requests",
        sa.Column("payment_method", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("quote_requests", "payment_method")
