"""b2c price uploads

Revision ID: 447bcf99659b
Revises: 019
Create Date: 2026-05-29 11:42:33.236747
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '447bcf99659b'
down_revision: Union[str, None] = '019'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Solo crea la tabla b2c_price_uploads. El resto del autogenerate detectaba
    # drift preexistente NO relacionado (índices/FKs/columnas de otras tablas)
    # y se descartó a propósito para no alterar el esquema vigente.
    op.create_table('b2c_price_uploads',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('source_filename', sa.String(length=512), nullable=True),
    sa.Column('s3_bucket', sa.String(length=255), nullable=True),
    sa.Column('s3_key', sa.String(length=1024), nullable=True),
    sa.Column('status', sa.String(length=32), server_default='PENDING', nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('shop_domain', sa.String(length=255), nullable=True),
    sa.Column('parsed_items', sa.Integer(), server_default='0', nullable=False),
    sa.Column('variants_matched', sa.Integer(), server_default='0', nullable=False),
    sa.Column('variants_updated', sa.Integer(), server_default='0', nullable=False),
    sa.Column('variants_already_ok', sa.Integer(), server_default='0', nullable=False),
    sa.Column('variants_missing', sa.Integer(), server_default='0', nullable=False),
    sa.Column('missing_skus_sample', sa.Text(), nullable=True),
    sa.Column('uploaded_by_user_id', sa.Uuid(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('PENDING','PROCESSING','COMPLETED','PARTIAL','FAILED')", name='ck_b2c_price_uploads_status'),
    sa.ForeignKeyConstraint(['uploaded_by_user_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('b2c_price_uploads')
