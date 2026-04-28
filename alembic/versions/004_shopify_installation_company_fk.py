"""shopify_app_installations.company_id — CRM company per shop install

Revision ID: 004
Revises: 003
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "shopify_app_installations",
        sa.Column("company_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_shopify_app_installations_company",
        "shopify_app_installations",
        "companies",
        ["company_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_shopify_app_installations_company",
        "shopify_app_installations",
        type_="foreignkey",
    )
    op.drop_column("shopify_app_installations", "company_id")
