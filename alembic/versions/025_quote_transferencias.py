"""Transferencias APRO IA en quote_requests + estado ANULADA.

Agrega el tracking del documento de transferencia que APRO IA reporta por
webhook (folio propio, estado y momento en que el pedido quedó pagado) y
habilita el estado terminal ``ANULADA`` para las cotizaciones cuya
transferencia quedó descartada/anulada.

Revision ID: 025
Revises: 024
Create Date: 2026-08-17
"""
from alembic import op
import sqlalchemy as sa

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None

_CK_NAME = "ck_quote_requests_status"
_STATUSES_NUEVOS = (
    "'SOLICITADA', 'COTIZADA', 'INVOICE_SENT', 'ACEPTADA', "
    "'RECHAZADA', 'EXPIRADA', 'ANULADA'"
)
_STATUSES_VIEJOS = (
    "'SOLICITADA', 'COTIZADA', 'INVOICE_SENT', 'ACEPTADA', "
    "'RECHAZADA', 'EXPIRADA'"
)


def upgrade() -> None:
    op.add_column(
        "quote_requests",
        sa.Column("transfer_folio", sa.String(64), nullable=True),
    )
    op.add_column(
        "quote_requests",
        sa.Column("transfer_status", sa.String(64), nullable=True),
    )
    op.add_column(
        "quote_requests",
        sa.Column("transfer_paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_quote_requests_transfer_folio", "quote_requests", ["transfer_folio"]
    )
    op.drop_constraint(_CK_NAME, "quote_requests", type_="check")
    op.create_check_constraint(
        _CK_NAME, "quote_requests", f"status IN ({_STATUSES_NUEVOS})"
    )


def downgrade() -> None:
    # Las cotizaciones ya anuladas no caben en el constraint anterior; se
    # devuelven a RECHAZADA para poder recrearlo.
    op.execute(
        "UPDATE quote_requests SET status = 'RECHAZADA' WHERE status = 'ANULADA'"
    )
    op.drop_constraint(_CK_NAME, "quote_requests", type_="check")
    op.create_check_constraint(
        _CK_NAME, "quote_requests", f"status IN ({_STATUSES_VIEJOS})"
    )
    op.drop_index("ix_quote_requests_transfer_folio", table_name="quote_requests")
    op.drop_column("quote_requests", "transfer_paid_at")
    op.drop_column("quote_requests", "transfer_status")
    op.drop_column("quote_requests", "transfer_folio")
