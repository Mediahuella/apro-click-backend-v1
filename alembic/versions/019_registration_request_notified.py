"""registration_request_notified — marca de envío de notificaciones por email

Agrega dos columnas nullable a `company_registration_requests`:

- `approval_notified_at` (TIMESTAMPTZ): timestamp de cuando se encoló el email de
  aprobación del cliente. NULL = nunca se notificó (típicamente, aprobaciones
  hechas antes de habilitar SES).
- `rejection_notified_at` (TIMESTAMPTZ): equivalente para rechazos.

Estas columnas son la fuente de verdad del backfill `scripts/notify_approved_backfill.py`:
solo se notifican filas con el flag en NULL.

Revision ID: 019
Revises: 018
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "019"
down_revision: Union[str, None] = "018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "company_registration_requests",
        sa.Column("approval_notified_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "company_registration_requests",
        sa.Column("rejection_notified_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("company_registration_requests", "rejection_notified_at")
    op.drop_column("company_registration_requests", "approval_notified_at")
