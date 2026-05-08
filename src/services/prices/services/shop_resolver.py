"""Resuelve ``(shop_domain, access_token)`` desde la BD.

Pequeña utilidad reutilizada por los handlers/worker del servicio prices.
Aplica la misma lógica que ``orders/services/shopify_inventory_metafield.py``:

1. Si la variable ``SHOPIFY_SHOP`` está seteada, se busca esa instalación.
2. Si no, se toma la única instalación activa.
3. Lanza ``LookupError`` si no encuentra una con token persistido.
"""
from __future__ import annotations

import os
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

from sqlalchemy import select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.shopify import ShopifyAppInstallation  # noqa: E402


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


def resolve_shop_and_token(shop_domain: str | None = None) -> tuple[str, str]:
    """Devuelve ``(shop_domain, access_token)``.

    - Si ``shop_domain`` (arg o env ``SHOPIFY_SHOP``) está seteado, busca esa
      instalación.
    - Si no, usa la única instalación con ``uninstalled_at IS NULL`` y token
      poblado. Si hay más de una, falla por seguridad.
    """
    domain = (shop_domain or os.environ.get("SHOPIFY_SHOP") or "").strip()
    with get_session() as session:
        if domain:
            dom = _normalize_shop_domain(domain)
            row = session.scalar(
                select(ShopifyAppInstallation).where(
                    ShopifyAppInstallation.shop_domain == dom,
                    ShopifyAppInstallation.uninstalled_at.is_(None),
                )
            )
        else:
            rows = list(
                session.scalars(
                    select(ShopifyAppInstallation).where(
                        ShopifyAppInstallation.uninstalled_at.is_(None),
                        ShopifyAppInstallation.shopify_access_token.is_not(None),
                    )
                )
            )
            if len(rows) > 1:
                raise LookupError(
                    "Hay más de una instalación Shopify activa. "
                    "Defina SHOPIFY_SHOP para elegir la tienda principal."
                )
            row = rows[0] if rows else None

    if not row or not row.shopify_access_token:
        raise LookupError(
            "No hay instalación Shopify activa con token. "
            "Verifique la tabla shopify_app_installations o SHOPIFY_SHOP."
        )
    return row.shop_domain, row.shopify_access_token.strip()
