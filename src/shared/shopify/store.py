"""Resolución de tienda Shopify activa → (shop_domain, access_token).

Encapsula el patrón repetido en varios servicios: buscar la
``ShopifyAppInstallation`` activa (no desinstalada) y devolver el dominio + el
offline access token con el que llamar a la Admin API.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models.shopify import ShopifyAppInstallation


class StoreNotResolvedError(RuntimeError):
    """No hay instalación Shopify activa para resolver tienda/token."""


def resolve_store_token(
    session: Session, shop_domain: str | None = None
) -> tuple[str, str]:
    """Devuelve ``(shop_domain, access_token)`` de una instalación activa.

    - Si ``shop_domain`` se pasa, busca esa instalación específica.
    - Si no, toma la única instalación activa (``uninstalled_at IS NULL``).

    Lanza :class:`StoreNotResolvedError` si no encuentra instalación o token.
    """
    stmt = select(ShopifyAppInstallation).where(
        ShopifyAppInstallation.uninstalled_at.is_(None)
    )
    if shop_domain:
        stmt = stmt.where(
            ShopifyAppInstallation.shop_domain == shop_domain.strip().lower()
        )
    stmt = stmt.order_by(ShopifyAppInstallation.installed_at.desc())
    inst = session.scalars(stmt).first()
    if not inst:
        raise StoreNotResolvedError(
            "No hay instalación Shopify activa"
            + (f" para {shop_domain}" if shop_domain else "")
        )
    token = (inst.shopify_access_token or "").strip()
    if not token:
        raise StoreNotResolvedError(
            f"Instalación {inst.shop_domain} sin access token"
        )
    return inst.shop_domain, token
