"""conversations.closed_at — marca de cierre (staff).

Revision ID: 013
Revises: 012
Create Date: 2026-04-28
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: Union[str, None] = "012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE conversations SET closed_at = updated_at "
            "WHERE status = 'CLOSED' AND closed_at IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("conversations", "closed_at")
