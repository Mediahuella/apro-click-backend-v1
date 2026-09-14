"""Espejo QA de APRO IA en quote_requests.

Agrega los identificadores del espejo de la cotización en el ambiente QA del
DMS (``dms-qa.apro.cl``). Van en columnas propias y no reusan
``apro_ia_id``/``apro_ia_folio`` porque QA y producción son secuencias de IDs
independientes: un ``id`` de QA puede colisionar con el de una cotización real
de otro cliente, y el matching del webhook de eventos es por ``apro_ia_id``.

Revision ID: 026
Revises: 025
Create Date: 2026-08-19
"""
from alembic import op
import sqlalchemy as sa

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "quote_requests",
        sa.Column("apro_ia_qa_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "quote_requests",
        sa.Column("apro_ia_qa_folio", sa.String(length=20), nullable=True),
    )
    op.create_index(
        "ix_quote_requests_apro_ia_qa_id", "quote_requests", ["apro_ia_qa_id"]
    )
    op.create_index(
        "ix_quote_requests_apro_ia_qa_folio", "quote_requests", ["apro_ia_qa_folio"]
    )


def downgrade() -> None:
    op.drop_index("ix_quote_requests_apro_ia_qa_folio", table_name="quote_requests")
    op.drop_index("ix_quote_requests_apro_ia_qa_id", table_name="quote_requests")
    op.drop_column("quote_requests", "apro_ia_qa_folio")
    op.drop_column("quote_requests", "apro_ia_qa_id")
