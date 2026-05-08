"""Sincronización del metafield ``aproclick.stock`` por variante.

Este módulo es invocado por el worker SQS que consume eventos
``inventory_levels/update``. Suma el stock disponible entre todas las
locations activas y escribe ``metafieldsSet`` en la variante asociada al
``InventoryItem``.

La function `manejo-de-pagos` lee ese metafield para decidir qué métodos de
pago mostrar en checkout (ver ``docs/SYNC_STOCK_PAYMENT_CUSTOMIZATION.md``).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

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

from database.engine import get_session  # noqa: E402
from database.models.shopify import ShopifyAppInstallation  # noqa: E402
from utils.shopify_graphql import format_user_errors, graphql_call  # noqa: E402

_INVENTORY_ITEM_QUERY = """
query InventoryItemStock($id: ID!) {
  inventoryItem(id: $id) {
    id
    variant { id }
    inventoryLevels(first: 50) {
      edges {
        node {
          location { id isActive }
          quantities(names: ["available"]) { quantity }
        }
      }
    }
  }
}
"""

_METAFIELDS_SET = """
mutation StockMetafieldSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    userErrors { field message code }
  }
}
"""


@dataclass(frozen=True)
class SyncResult:
    """Resultado del intento de sync para un ``inventory_item_id``."""

    inventory_item_id: str
    variant_gid: str | None
    total_available: int
    written: bool
    skipped_reason: str | None = None


def stock_namespace() -> str:
    return (os.environ.get("SHOPIFY_STOCK_NAMESPACE") or "aproclick").strip()


def stock_key() -> str:
    return (os.environ.get("SHOPIFY_STOCK_KEY") or "stock").strip()


def _normalize_shop_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    if not d:
        raise ValueError("shop_domain vacío")
    if not d.endswith(".myshopify.com") and "." in d:
        raise ValueError(
            "Dominio de tienda inválido: use el formato tienda.myshopify.com"
        )
    if not d.endswith(".myshopify.com"):
        d = f"{d}.myshopify.com"
    return d


def resolve_shop_and_token(shop_domain: str) -> tuple[str, str]:
    """Devuelve ``(shop_domain, access_token)`` desde la tabla de instalaciones.

    Lanza ``LookupError`` si la tienda no tiene una instalación activa con
    token persistido.
    """
    dom = _normalize_shop_domain(shop_domain)
    with get_session() as session:
        row = session.scalar(
            select(ShopifyAppInstallation).where(
                ShopifyAppInstallation.shop_domain == dom,
                ShopifyAppInstallation.uninstalled_at.is_(None),
            )
        )
    if not row or not row.shopify_access_token:
        raise LookupError(
            f"No hay instalación activa con token para la tienda '{dom}'"
        )
    return row.shop_domain, row.shopify_access_token.strip()


def _coerce_inventory_item_id(raw: object) -> str:
    if raw is None:
        raise ValueError("inventory_item_id requerido")
    s = str(raw).strip()
    if not s:
        raise ValueError("inventory_item_id vacío")
    if s.startswith("gid://shopify/InventoryItem/"):
        return s
    if not s.isdigit():
        raise ValueError(f"inventory_item_id inválido: {raw!r}")
    return f"gid://shopify/InventoryItem/{s}"


def sync_variant_stock_from_inventory_item(
    shop_domain: str,
    access_token: str,
    inventory_item_id: object,
) -> SyncResult:
    """Resuelve la variante y escribe ``aproclick.stock`` con el total available.

    - Si el item no tiene variante asociada, hace no-op.
    - Si Shopify devuelve ``userErrors`` en ``metafieldsSet``, lanza ``ValueError``.
    - Errores de red/HTTP suben como ``ValueError`` desde ``graphql_call``.
    """
    item_gid = _coerce_inventory_item_id(inventory_item_id)

    data = graphql_call(
        shop_domain,
        access_token,
        _INVENTORY_ITEM_QUERY,
        {"id": item_gid},
    )
    item = data.get("inventoryItem") or {}
    variant = item.get("variant") if isinstance(item, dict) else None
    variant_gid = (
        variant.get("id") if isinstance(variant, dict) and variant.get("id") else None
    )
    if not variant_gid:
        return SyncResult(
            inventory_item_id=item_gid,
            variant_gid=None,
            total_available=0,
            written=False,
            skipped_reason="no_variant",
        )

    levels_edges = (item.get("inventoryLevels") or {}).get("edges") or []
    total = 0
    for edge in levels_edges:
        node = (edge or {}).get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        loc = node.get("location") or {}
        if not isinstance(loc, dict) or not loc.get("isActive"):
            continue
        for q in node.get("quantities") or []:
            if not isinstance(q, dict):
                continue
            try:
                total += int(q.get("quantity") or 0)
            except (TypeError, ValueError):
                continue

    set_data = graphql_call(
        shop_domain,
        access_token,
        _METAFIELDS_SET,
        {
            "metafields": [
                {
                    "ownerId": variant_gid,
                    "namespace": stock_namespace(),
                    "key": stock_key(),
                    "type": "number_integer",
                    "value": str(total),
                }
            ]
        },
    )
    payload = set_data.get("metafieldsSet") or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        raise ValueError(
            "metafieldsSet falló: " + format_user_errors(user_errors)
        )

    return SyncResult(
        inventory_item_id=item_gid,
        variant_gid=variant_gid,
        total_available=total,
        written=True,
    )
