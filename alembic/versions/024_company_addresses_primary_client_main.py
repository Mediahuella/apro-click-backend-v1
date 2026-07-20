"""company_addresses: unicidad de is_primary; clients: is_main_contact + email único

Revision ID: 024
Revises: 0e4555d5791a
Create Date: 2026-07-14 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '024'
down_revision: Union[str, None] = '0e4555d5791a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        'uq_company_addresses_primary_per_company',
        'company_addresses',
        ['company_id'],
        unique=True,
        postgresql_where=sa.text('is_primary = true'),
    )

    op.add_column(
        'clients',
        sa.Column(
            'is_main_contact', sa.Boolean(), nullable=False, server_default=sa.text('false')
        ),
    )
    op.create_index(
        'uq_clients_main_contact_per_company',
        'clients',
        ['company_id'],
        unique=True,
        postgresql_where=sa.text('is_main_contact = true'),
    )

    op.create_index(
        'uq_clients_company_email',
        'clients',
        ['company_id', 'email'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index('uq_clients_company_email', table_name='clients')
    op.drop_index('uq_clients_main_contact_per_company', table_name='clients')
    op.drop_column('clients', 'is_main_contact')
    op.drop_index('uq_company_addresses_primary_per_company', table_name='company_addresses')
