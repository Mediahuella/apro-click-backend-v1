"""Resolución de `note_attributes` desde `companies` + vendedor (`users.codigo_sap`)."""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import exists, or_, select

from database.engine import get_session
from database.models.company import Company
from database.models.shopify import ShopifyAppInstallation
from database.models.user import User
from database.models.user_company import UserCompany

logger = logging.getLogger(__name__)


def normalize_shop_domain(shop: str) -> str:
    s = (shop or "").strip().lower()
    if not s:
        return ""
    if "://" in s:
        from urllib.parse import urlparse

        s = urlparse(s).hostname or ""
        s = s.strip().lower()
    s = s.removesuffix(".")
    if not s.endswith(".myshopify.com"):
        if "." not in s:
            s = f"{s}.myshopify.com"
    return s


_gid_company = re.compile(
    r"^gid://shopify/Company/(\d+)\s*$", re.IGNORECASE
)


def normalize_shopify_company_id(raw: str) -> str:
    """
    Acepta id numérico o GID `gid://shopify/Company/...`.
    Devuelve el id normalmente numérico como string.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    m = _gid_company.match(s)
    if m:
        return m.group(1)
    return s


def resolve_billing_for_checkout(
    shop_domain: str,
    shopify_company_id: str,
    checkout_token: str | None,
    campaign: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Resuelve datos de facturación CRM y el mapa para note attributes de Shopify.

    Returns:
        ``note_attributes``: claves con los nombres esperados en la orden Shopify.
        ``billing``: mismos datos con claves estables (snake_case) para la UI / logs.
    """
    _ = (checkout_token, campaign)
    empty: tuple[dict[str, str], dict[str, str]] = ({}, {})
    nid = normalize_shopify_company_id(shopify_company_id)
    if not nid:
        return empty

    with get_session() as session:
        company = session.execute(
            select(Company).where(Company.shopify_company_id == nid)
        ).scalar_one_or_none()

        if company is None:
            logger.info(
                "checkout billing: no company for shopify_company_id=%s", nid
            )
            return empty

        inst = session.execute(
            select(ShopifyAppInstallation).where(
                ShopifyAppInstallation.shop_domain == shop_domain
            )
        ).scalar_one_or_none()
        if inst and inst.company_id and inst.company_id != company.id:
            logger.warning(
                "checkout billing: shopify_company_id resolvió company %s pero "
                "shopify_app_installations.company_id es %s para shop %s",
                company.id,
                inst.company_id,
                shop_domain,
            )
            return empty

        billing_documento = company.billing_documento
        billing_rut = company.billing_rut
        billing_razon_social = company.billing_razon_social
        billing_giro = company.billing_giro
        billing_region = company.billing_region
        billing_direccion = company.billing_direccion
        company_name = company.name
        company_uuid = company.id

        uc_exists = exists().where(
            UserCompany.user_id == User.id,
            UserCompany.company_id == company_uuid,
        )
        seller = session.execute(
            select(User)
            .where(User.role == "SALES")
            .where(User.status == "ACTIVE")
            .where(User.codigo_sap.is_not(None))
            .where(User.codigo_sap != "")
            .where(or_(User.company_id == company_uuid, uc_exists))
            .order_by(User.codigo_sap.asc())
            .limit(1)
        ).scalar_one_or_none()
        vendedor_sap = (
            (seller.codigo_sap or "").strip() if seller else ""
        )

    note_attributes: dict[str, str] = {}
    if billing_documento:
        note_attributes["Documento"] = billing_documento.strip()
    if billing_rut:
        note_attributes["Rut"] = billing_rut.strip()
    razon = (billing_razon_social or "").strip() or (company_name or "").strip()
    if razon:
        note_attributes["Razon Social"] = razon
    if billing_giro:
        note_attributes["Giro"] = billing_giro.strip()
    if billing_region:
        note_attributes["Region"] = billing_region.strip()
    if billing_direccion:
        note_attributes["Dirección"] = billing_direccion.strip()

    if vendedor_sap:
        note_attributes["Vendedor"] = vendedor_sap

    billing: dict[str, str] = {}
    if billing_documento and billing_documento.strip():
        billing["documento"] = billing_documento.strip()
    if billing_rut and billing_rut.strip():
        billing["rut"] = billing_rut.strip()
    if razon:
        billing["razon_social"] = razon
    if billing_giro and billing_giro.strip():
        billing["giro"] = billing_giro.strip()
    if billing_region and billing_region.strip():
        billing["region"] = billing_region.strip()
    if billing_direccion and billing_direccion.strip():
        billing["direccion"] = billing_direccion.strip()
    if vendedor_sap:
        billing["vendedor"] = vendedor_sap

    return note_attributes, billing


def resolve_note_attributes(
    shop_domain: str,
    shopify_company_id: str,
    checkout_token: str | None,
    campaign: dict[str, Any],
) -> dict[str, str]:
    """Sólo el mapa para ``applyAttributeChange`` (retrocompatibilidad interna)."""
    attrs, _ = resolve_billing_for_checkout(
        shop_domain, shopify_company_id, checkout_token, campaign
    )
    return attrs
