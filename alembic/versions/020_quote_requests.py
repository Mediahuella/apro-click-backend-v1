"""quote requests (cotizaciones B2B)

Revision ID: 020
Revises: 447bcf99659b
Create Date: 2026-06-04 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '020'
down_revision: Union[str, None] = '447bcf99659b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Secuencia para el folio CO-NNNNNN (correlativo sin colisiones).
    op.execute("CREATE SEQUENCE IF NOT EXISTS quote_folio_seq START WITH 1")

    op.create_table(
        'quote_requests',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('folio', sa.String(length=32), nullable=True),
        sa.Column('company_id', sa.Uuid(), nullable=True),
        sa.Column('shopify_company_id', sa.String(), nullable=True),
        sa.Column('company_location_gid', sa.String(), nullable=True),
        sa.Column('shop_domain', sa.String(length=255), nullable=False),
        sa.Column('requested_by_email', sa.String(length=255), nullable=True),
        sa.Column('requested_by_customer_id', sa.String(length=128), nullable=True),
        sa.Column('requested_by_name', sa.String(length=255), nullable=True),
        sa.Column('status', sa.String(length=16), server_default='SOLICITADA', nullable=False),
        sa.Column('draft_order_gid', sa.String(), nullable=True),
        sa.Column('order_gid', sa.String(), nullable=True),
        sa.Column('order_applied_discount', JSONB(), nullable=True),
        sa.Column('payment_terms_label', sa.String(length=128), nullable=True),
        sa.Column('validity_days', sa.Integer(), server_default='14', nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('staff_note', sa.Text(), nullable=True),
        sa.Column('pdf_s3_bucket', sa.String(), nullable=True),
        sa.Column('pdf_s3_key', sa.String(), nullable=True),
        sa.Column('pdf_generated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            "status IN ('SOLICITADA','COTIZADA','INVOICE_SENT','ACEPTADA','RECHAZADA','EXPIRADA')",
            name='ck_quote_requests_status',
        ),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('folio', name='uq_quote_requests_folio'),
    )
    op.create_index('ix_quote_requests_company_id', 'quote_requests', ['company_id'])
    op.create_index('ix_quote_requests_shop_domain', 'quote_requests', ['shop_domain'])
    op.create_index('ix_quote_requests_status', 'quote_requests', ['status'])
    op.create_index('ix_quote_requests_draft_order_gid', 'quote_requests', ['draft_order_gid'])
    op.create_index('ix_quote_requests_order_gid', 'quote_requests', ['order_gid'])

    op.create_table(
        'quote_request_lines',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('quote_request_id', sa.Uuid(), nullable=False),
        sa.Column('line_number', sa.Integer(), nullable=False),
        sa.Column('variant_gid', sa.String(), nullable=False),
        sa.Column('sku', sa.String(length=128), nullable=True),
        sa.Column('title', sa.String(length=512), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('on_demand', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('requested_unit_price', sa.Numeric(18, 4), nullable=True),
        sa.Column('negotiated_unit_price', sa.Numeric(18, 4), nullable=True),
        sa.Column('applied_discount', JSONB(), nullable=True),
        sa.Column('currency', sa.String(length=8), server_default='CLP', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['quote_request_id'], ['quote_requests.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_quote_request_lines_quote_request_id',
        'quote_request_lines',
        ['quote_request_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_quote_request_lines_quote_request_id', table_name='quote_request_lines')
    op.drop_table('quote_request_lines')
    for ix in (
        'ix_quote_requests_order_gid',
        'ix_quote_requests_draft_order_gid',
        'ix_quote_requests_status',
        'ix_quote_requests_shop_domain',
        'ix_quote_requests_company_id',
    ):
        op.drop_index(ix, table_name='quote_requests')
    op.drop_table('quote_requests')
    op.execute("DROP SEQUENCE IF EXISTS quote_folio_seq")
