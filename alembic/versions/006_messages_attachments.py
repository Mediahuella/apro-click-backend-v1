"""messages: message_type + attachment_key (soporte adjuntos)

Revision ID: 006
Revises: 005
Create Date: 2026-04-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "message_type",
            sa.String(20),
            nullable=False,
            server_default="TEXT",
        ),
    )
    op.add_column(
        "messages",
        sa.Column("attachment_key", sa.String(512), nullable=True),
    )
    op.alter_column("messages", "body", existing_type=sa.Text(), nullable=True)

    op.create_check_constraint(
        "ck_messages_type",
        "messages",
        "message_type IN ('TEXT', 'IMAGE', 'FILE')",
    )
    op.create_check_constraint(
        "ck_messages_body_or_attachment",
        "messages",
        "(body IS NOT NULL) OR (attachment_key IS NOT NULL)",
    )
    op.create_index(
        "ix_messages_conversation_created",
        "messages",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_constraint("ck_messages_body_or_attachment", "messages", type_="check")
    op.drop_constraint("ck_messages_type", "messages", type_="check")
    op.alter_column("messages", "body", existing_type=sa.Text(), nullable=False)
    op.drop_column("messages", "attachment_key")
    op.drop_column("messages", "message_type")
