"""shopify_access_token — token Admin API en PostgreSQL

Revision ID: 002
Revises: 001
Create Date: 2026-04-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "shopify_app_installations",
        sa.Column("shopify_access_token", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shopify_app_installations", "shopify_access_token")
