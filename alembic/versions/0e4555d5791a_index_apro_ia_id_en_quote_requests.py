"""index apro_ia_id en quote_requests

Revision ID: 0e4555d5791a
Revises: 023
Create Date: 2026-07-09 11:47:58.938406
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0e4555d5791a'
down_revision: Union[str, None] = '023'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        op.f('ix_quote_requests_apro_ia_id'),
        'quote_requests',
        ['apro_ia_id'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_quote_requests_apro_ia_id'), table_name='quote_requests')
