"""Varios hilos por cliente+empresa; a lo sumo uno OPEN (cierra duplicados OPEN previos).

Revision ID: 012
Revises: 011
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                    ROW_NUMBER() OVER (
                        PARTITION BY company_id, client_id
                        ORDER BY COALESCE(last_message_at, created_at) DESC NULLS LAST,
                            created_at DESC
                    ) AS rn
                FROM conversations
                WHERE status = 'OPEN'
            )
            UPDATE conversations c
            SET status = 'CLOSED', updated_at = NOW()
            FROM ranked r
            WHERE c.id = r.id AND r.rn > 1
            """
        )
    )

    op.drop_index("uq_conversations_unassigned_client", table_name="conversations")
    op.drop_index("uq_conversations_seller_client", table_name="conversations")

    op.create_index(
        "uq_conversations_company_client_open",
        "conversations",
        ["company_id", "client_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )


def downgrade() -> None:
    op.drop_index("uq_conversations_company_client_open", table_name="conversations")

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
