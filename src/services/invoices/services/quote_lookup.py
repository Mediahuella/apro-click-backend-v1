"""Resuelve las Órdenes de Compra (folio APRO IA) de una empresa B2B.

Cada ``QuoteRequest`` que fue sincronizada con APRO IA (Draft Order negociado)
tiene un ``apro_ia_folio`` (formato ``AC-NNNNNN``) — ese es el folio de OC que
usa el DMS externo para la consulta de facturas. Cuando el Draft Order se
convirtió en pedido (``order_gid``), también resolvemos el número de pedido
Shopify (``ShopifyOrder.order_name``, ej. ``#1061``) para mostrarlo junto a
la factura.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

lambda_root = "/var/task"
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

possible_shared_paths = [
    Path("/var/task/shared"),
    service_root / "shared",
]
for path in possible_shared_paths:
    if path.exists() and (path / "database").exists():
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        break

from database.engine import get_session
from database.models.quote_request import QuoteRequest
from database.models.shopify_order import ShopifyOrder
from sqlalchemy import select

_ORDER_GID_RE = re.compile(r"(\d+)\s*$")


def _order_numeric_id(order_gid: str | None) -> str | None:
    if not order_gid:
        return None
    m = _ORDER_GID_RE.search(order_gid)
    return m.group(1) if m else None


def list_oc_folios(
    shopify_company_id: str, *, limit: int = 20
) -> list[dict[str, str | None]]:
    """Devuelve las OC más recientes de la empresa.

    ``[{quote_folio, oc_folio, order_name}]`` — ``order_name`` (ej. ``#1061``)
    va ``None`` si la cotización todavía no se convirtió en pedido Shopify.
    """
    shopify_company_id = str(shopify_company_id or "").strip()
    if not shopify_company_id:
        return []
    with get_session() as session:
        rows = session.scalars(
            select(QuoteRequest)
            .where(
                QuoteRequest.shopify_company_id == shopify_company_id,
                QuoteRequest.apro_ia_folio.is_not(None),
            )
            .order_by(QuoteRequest.created_at.desc())
            .limit(limit)
        ).all()
        rows = [q for q in rows if q.apro_ia_folio]

        order_ids = {
            oid
            for oid in (_order_numeric_id(q.order_gid) for q in rows)
            if oid
        }
        order_names: dict[str, str] = {}
        if order_ids:
            orders = session.scalars(
                select(ShopifyOrder).where(
                    ShopifyOrder.shopify_order_id.in_(order_ids)
                )
            ).all()
            order_names = {o.shopify_order_id: o.order_name for o in orders}

        return [
            {
                "quote_folio": q.folio,
                "oc_folio": q.apro_ia_folio,
                "order_name": order_names.get(_order_numeric_id(q.order_gid) or ""),
            }
            for q in rows
        ]
