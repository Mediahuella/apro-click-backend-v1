"""billing_documento_default_factura

Revision ID: 54fd1b83794a
Revises: 016
Create Date: 2026-05-18 15:56:41.358565
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '54fd1b83794a'
down_revision: Union[str, None] = '016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE companies SET billing_documento = 'Factura' WHERE billing_documento IS NULL")
    op.alter_column(
        'companies', 'billing_documento',
        existing_type=sa.VARCHAR(length=64),
        nullable=False,
        server_default='Factura',
    )


def downgrade() -> None:
    op.alter_column(
        'companies', 'billing_documento',
        existing_type=sa.VARCHAR(length=64),
        nullable=True,
        server_default=None,
    )
