"""initial_schema

Revision ID: 001
Revises:
Create Date: 2026-04-14
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- companies ---
    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("company_type", sa.String(), nullable=False),
        sa.Column("payment_type", sa.String(), nullable=False),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("company_type IN ('SMALL', 'MEDIUM', 'BIG')", name="ck_companies_company_type"),
        sa.CheckConstraint("payment_type IN ('DIRECT', 'CREDIT')", name="ck_companies_payment_type"),
    )

    # --- shopify_app_installations ---
    op.create_table(
        "shopify_app_installations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("shop_domain", sa.String(), nullable=False),
        sa.Column("access_token_secret_id", sa.String(), nullable=True),
        sa.Column("scopes", sa.String(), nullable=True),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uninstalled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shop_domain"),
    )

    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cognito_sub", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("given_name", sa.String(), nullable=False, server_default=""),
        sa.Column("family_name", sa.String(), nullable=False, server_default=""),
        sa.Column("role", sa.String(), nullable=False, server_default="SALES"),
        sa.Column("status", sa.String(), nullable=False, server_default="PENDING"),
        sa.Column("company_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cognito_sub"),
        sa.UniqueConstraint("email"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.CheckConstraint("role IN ('SUPERADMIN', 'ADMIN', 'SALES', 'KPI_VISUALIZERS')", name="ck_users_role"),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED', 'PENDING')", name="ck_users_status"),
    )

    # --- clients ---
    op.create_table(
        "clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("shopify_customer_id", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.UniqueConstraint("company_id", "shopify_customer_id", name="uq_clients_company_shopify"),
    )

    # --- audit_logs ---
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("metadata", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
    )

    # --- company_registration_requests ---
    op.create_table(
        "company_registration_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="PENDING"),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("submitted_email", sa.String(), nullable=True),
        sa.Column("resolved_company_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["resolved_company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"]),
        sa.CheckConstraint("status IN ('PENDING', 'APPROVED', 'REJECTED', 'NEEDS_INFO')", name="ck_reg_requests_status"),
    )

    # --- conversations ---
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("seller_user_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="OPEN"),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["seller_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.UniqueConstraint("seller_user_id", "client_id", name="uq_conversations_seller_client"),
        sa.CheckConstraint("status IN ('OPEN', 'CLOSED')", name="ck_conversations_status"),
    )

    # --- messages ---
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("sender_type", sa.String(), nullable=False),
        sa.Column("sender_user_id", sa.Uuid(), nullable=True),
        sa.Column("sender_client_id", sa.Uuid(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sender_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["sender_client_id"], ["clients.id"]),
        sa.CheckConstraint("sender_type IN ('USER', 'CLIENT')", name="ck_messages_sender_type"),
        sa.CheckConstraint(
            "(sender_type = 'USER' AND sender_user_id IS NOT NULL AND sender_client_id IS NULL) OR "
            "(sender_type = 'CLIENT' AND sender_client_id IS NOT NULL AND sender_user_id IS NULL)",
            name="ck_messages_sender_consistency",
        ),
    )

    # --- seed: Apro system company ---
    op.execute(
        "INSERT INTO companies (id, name, company_type, payment_type, is_system) "
        "VALUES ('00000000-0000-0000-0000-000000000001', 'Apro Click', 'SMALL', 'DIRECT', true)"
    )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("company_registration_requests")
    op.drop_table("audit_logs")
    op.drop_table("clients")
    op.drop_table("users")
    op.drop_table("shopify_app_installations")
    op.drop_table("companies")
