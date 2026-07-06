"""Credenciales de la tienda Shopify B2C (``aproltda``).

A diferencia de la tienda B2B (``aproclick``), cuyo token se resuelve desde la
BD (``shopify_app_installations``), la tienda B2C es una tienda distinta y su
token vive **solo en variables de entorno del Lambda** (nunca en BD ni en el
frontend). Se setean en el ``serverless.yml`` del servicio ``prices`` a partir
del ``.env`` (``APRO_B2C_URL`` / ``APRO_B2C_TOKEN`` / ``APRO_B2C_VERSION``).
"""
from __future__ import annotations

import os


def resolve_b2c_shop_and_token() -> tuple[str, str, str]:
    """Devuelve ``(shop_domain, access_token, api_version)`` de la tienda B2C.

    Lanza ``RuntimeError`` con un mensaje accionable si falta configuración.
    """
    shop = (os.environ.get("B2C_SHOPIFY_SHOP") or "").strip()
    token = (os.environ.get("B2C_SHOPIFY_ADMIN_TOKEN") or "").strip()
    api_version = (os.environ.get("B2C_SHOPIFY_API_VERSION") or "2026-04").strip()

    if not shop or not token:
        raise RuntimeError(
            "Faltan B2C_SHOPIFY_SHOP y/o B2C_SHOPIFY_ADMIN_TOKEN en el entorno "
            "del Lambda (se setean en serverless.yml desde APRO_B2C_URL / "
            "APRO_B2C_TOKEN)."
        )

    shop = shop.removeprefix("https://").strip().rstrip("/").lower()
    return shop, token, api_version
