"""shopify_orders: line_items con id/product_id/variant_id como strings en JSONB

Los valores numéricos en JSON se guardan como número en PostgreSQL; esta migración
reescribe filas existentes para que esas claves sean strings JSON ("123").

Revision ID: 009
Revises: 008
Create Date: 2026-04-27
"""
from __future__ import annotations

import json
from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "009"
down_revision: Union[str, None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STRING_ID_KEYS = ("id", "product_id", "variant_id")


def _normalize_line_items(items: Any) -> tuple[list[Any] | None, bool]:
    if items is None or not isinstance(items, list):
        return None, False
    changed = False
    out: list[Any] = []
    for li in items:
        if not isinstance(li, dict):
            out.append(li)
            continue
        d = dict(li)
        for key in _STRING_ID_KEYS:
            if key not in d:
                continue
            v = d[key]
            if v is None:
                continue
            if not isinstance(v, str):
                d[key] = str(v)
                changed = True
        out.append(d)
    if not changed:
        return None, False
    return out, True


def upgrade() -> None:
    conn = op.get_bind()
    dtype = conn.execute(
        sa.text(
            """
            SELECT data_type FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'shopify_orders'
              AND column_name = 'shopify_order_id'
            """
        )
    ).scalar()
    if dtype == "bigint":
        op.drop_constraint(
            "uq_shopify_orders_shop_order", "shopify_orders", type_="unique"
        )
        op.execute(
            sa.text(
                "ALTER TABLE shopify_orders ALTER COLUMN shopify_order_id "
                "TYPE VARCHAR(128) USING shopify_order_id::text"
            )
        )
        op.create_unique_constraint(
            "uq_shopify_orders_shop_order",
            "shopify_orders",
            ["shop_domain", "shopify_order_id"],
        )
    has_line_items = conn.execute(
        sa.text(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'shopify_orders'
              AND column_name = 'line_items'
            """
        )
    ).scalar()
    if not has_line_items:
        op.add_column(
            "shopify_orders",
            sa.Column("line_items", JSONB(), nullable=True),
        )

    rows = conn.execute(
        sa.text("SELECT id, line_items FROM shopify_orders WHERE line_items IS NOT NULL")
    ).fetchall()
    for row in rows:
        pk, li = row[0], row[1]
        new_li, changed = _normalize_line_items(li)
        if not changed or new_li is None:
            continue
        conn.execute(
            sa.text(
                "UPDATE shopify_orders SET line_items = CAST(:payload AS jsonb) "
                "WHERE id = :id"
            ),
            {"payload": json.dumps(new_li), "id": pk},
        )


def downgrade() -> None:
    pass
