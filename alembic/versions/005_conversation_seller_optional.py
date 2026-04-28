"""conversations.seller_user_id nullable — hilo en cola sin vendedor asignado

Revision ID: 005
Revises: 004
Create Date: 2026-04-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("uq_conversations_seller_client", "conversations", type_="unique")
    op.alter_column(
        "conversations",
        "seller_user_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.create_index(
        "uq_conversations_unassigned_client",
        "conversations",
        ["company_id", "client_id"],
        unique=True,
        postgresql_where=sa.text("seller_user_id IS NULL"),
    )
    op.create_index(
        "uq_conversations_seller_client",
        "conversations",
        ["seller_user_id", "client_id"],
        unique=True,
        postgresql_where=sa.text("seller_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_conversations_seller_client", table_name="conversations")
    op.drop_index("uq_conversations_unassigned_client", table_name="conversations")
    op.alter_column(
        "conversations",
        "seller_user_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_conversations_seller_client",
        "conversations",
        ["seller_user_id", "client_id"],
    )
