"""Estado ``PAGO_RECHAZADO`` para las cotizaciones con pago rechazado en APRO IA.

Antes, un pago ``Descartada``/``Anulada`` anulaba el pedido en Shopify y dejaba
la cotización en ``ANULADA``. Ahora el pedido queda abierto y pendiente de pago
(solo etiquetado), así que la cotización necesita un estado propio, visible para
los vendedores, que no se confunda ni con ``ANULADA`` (anulación real) ni con
``RECHAZADA`` (el cliente rechazó la cotización).

Las filas ya existentes no se migran: fueron anulaciones de verdad —el pedido
está cancelado en Shopify— y ``ANULADA`` las sigue describiendo bien.

Revision ID: 030
Revises: 029
Create Date: 2026-09-14
"""
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None

_CK_NAME = "ck_quote_requests_status"
_STATUSES_NUEVOS = (
    "'SOLICITADA', 'COTIZADA', 'INVOICE_SENT', 'ACEPTADA', "
    "'RECHAZADA', 'EXPIRADA', 'ANULADA', 'PAGO_RECHAZADO'"
)
_STATUSES_VIEJOS = (
    "'SOLICITADA', 'COTIZADA', 'INVOICE_SENT', 'ACEPTADA', "
    "'RECHAZADA', 'EXPIRADA', 'ANULADA'"
)


def upgrade() -> None:
    op.drop_constraint(_CK_NAME, "quote_requests", type_="check")
    op.create_check_constraint(
        _CK_NAME, "quote_requests", f"status IN ({_STATUSES_NUEVOS})"
    )


def downgrade() -> None:
    # El estado anterior más cercano es ANULADA: era el que fijaba este mismo
    # webhook antes del cambio.
    op.execute(
        "UPDATE quote_requests SET status = 'ANULADA' "
        "WHERE status = 'PAGO_RECHAZADO'"
    )
    op.drop_constraint(_CK_NAME, "quote_requests", type_="check")
    op.create_check_constraint(
        _CK_NAME, "quote_requests", f"status IN ({_STATUSES_VIEJOS})"
    )
