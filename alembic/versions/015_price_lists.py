"""price_list_uploads + shopify_price_segments: log de cargas Excel y catálogos B2B

Las listas de precio reales viven en Shopify como B2B Catalogs/PriceLists. En
Postgres sólo guardamos:

- ``price_list_uploads``: log de cada Excel subido por el admin (filename,
  fecha, autor, status del job, IDs de las bulk operations en Shopify).
- ``shopify_price_segments``: 1 fila por segmento (PYME / MEDIANA /
  GRAN_EMPRESA) con el GID del Catalog y el PriceList correspondientes en
  Shopify.

Revision ID: 015
Revises: 014
Create Date: 2026-05-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: Union[str, None] = "014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "price_list_uploads",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("source_filename", sa.String(length=512), nullable=True),
        sa.Column("s3_bucket", sa.String(length=255), nullable=True),
        sa.Column("s3_key", sa.String(length=1024), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "parsed_items",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "duplicates_overwritten",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "rows_skipped",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "variants_resolved",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "variants_missing",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "missing_skus_sample", sa.Text(), nullable=True,
            comment="Hasta ~50 SAPs sin variante en Shopify, separados por coma.",
        ),
        # IDs de las bulk operations en Shopify (uno por segmento).
        sa.Column(
            "pyme_bulk_operation_gid", sa.String(length=255), nullable=True
        ),
        sa.Column(
            "mediana_bulk_operation_gid", sa.String(length=255), nullable=True
        ),
        sa.Column(
            "gran_empresa_bulk_operation_gid",
            sa.String(length=255),
            nullable=True,
        ),
        sa.Column(
            "pyme_bulk_status", sa.String(length=32), nullable=True
        ),
        sa.Column(
            "mediana_bulk_status", sa.String(length=32), nullable=True
        ),
        sa.Column(
            "gran_empresa_bulk_status", sa.String(length=32), nullable=True
        ),
        sa.Column("uploaded_by_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_user_id"],
            ["users.id"],
            name="fk_price_list_uploads_uploaded_by_user",
            ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_price_list_uploads_status",
        "price_list_uploads",
        ["status"],
    )
    op.create_index(
        "ix_price_list_uploads_created_at",
        "price_list_uploads",
        [sa.text("created_at DESC")],
    )

    op.create_table(
        "shopify_price_segments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "segment", sa.String(length=32), nullable=False, unique=True,
            comment="PYME | MEDIANA | GRAN_EMPRESA",
        ),
        sa.Column("shop_domain", sa.String(length=255), nullable=False),
        sa.Column("catalog_gid", sa.String(length=255), nullable=True),
        sa.Column("price_list_gid", sa.String(length=255), nullable=True),
        sa.Column(
            "currency",
            sa.String(length=3),
            nullable=False,
            server_default=sa.text("'CLP'"),
        ),
        sa.Column(
            "company_location_ids",
            sa.Text(),
            nullable=True,
            comment="CSV de gid://shopify/CompanyLocation/... asociados al catálogo",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "segment IN ('PYME', 'MEDIANA', 'GRAN_EMPRESA')",
            name="ck_shopify_price_segments_segment",
        ),
    )


def downgrade() -> None:
    op.drop_table("shopify_price_segments")
    op.drop_index(
        "ix_price_list_uploads_created_at", table_name="price_list_uploads"
    )
    op.drop_index(
        "ix_price_list_uploads_status", table_name="price_list_uploads"
    )
    op.drop_table("price_list_uploads")
