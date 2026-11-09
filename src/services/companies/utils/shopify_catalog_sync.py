"""Sincronización de CompanyLocation ↔ Catálogos B2B en Shopify.

Se invoca desde ``CompanyService.update_company`` cuando cambia ``company_type``.
Quita la location de la empresa del catálogo del segmento anterior y la agrega
al catálogo del segmento nuevo (si los catálogos ya existen en Shopify).

Mapeo ``company_type`` → segmento B2B:
  SMALL  → PYME
  MEDIUM → MEDIANA
  BIG    → GRAN_EMPRESA
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger

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
from database.models.price_list import ShopifyPriceSegment  # noqa: E402
from database.models.shopify import ShopifyAppInstallation  # noqa: E402

from utils.shopify_graphql import format_user_errors, graphql_call  # noqa: E402

logger = Logger()

COMPANY_TYPE_TO_SEGMENT: dict[str, str] = {
    "SMALL": "PYME",
    "MEDIUM": "MEDIANA",
    "BIG": "GRAN_EMPRESA",
}

_COMPANY_LOCATIONS_PAGE = """
query CompanyLocs($id: ID!, $cursor: String) {
  company(id: $id) {
    id
    locations(first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { id }
    }
  }
}
"""

_CATALOG_CONTEXT_UPDATE = """
mutation CatalogContextUpdate(
  $catalogId: ID!,
  $contextsToAdd: CatalogContextInput,
  $contextsToRemove: CatalogContextInput
) {
  catalogContextUpdate(
    catalogId: $catalogId,
    contextsToAdd: $contextsToAdd,
    contextsToRemove: $contextsToRemove
  ) {
    catalog { id title status }
    userErrors { field message code }
  }
}
"""


def _resolve_shop_and_token() -> tuple[str, str]:
    """Devuelve ``(shop_domain, access_token)`` desde la BD."""
    shop_env = (os.environ.get("SHOPIFY_SHOP") or "").strip()
    with get_session() as session:
        if shop_env:
            domain = shop_env if shop_env.endswith(".myshopify.com") else f"{shop_env}.myshopify.com"
            row = session.scalar(
                select(ShopifyAppInstallation).where(
                    ShopifyAppInstallation.shop_domain == domain.lower(),
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
            row = rows[0] if len(rows) == 1 else None

    if not row or not row.shopify_access_token:
        raise LookupError("No hay instalación Shopify activa con token")
    return row.shop_domain, row.shopify_access_token.strip()


def _fetch_company_location_gids(
    shop_domain: str,
    access_token: str,
    company_id_raw: str,
) -> list[str]:
    """Devuelve todos los CompanyLocation GIDs de la empresa en Shopify."""
    raw = (company_id_raw or "").strip()
    if not raw:
        return []
    gid = raw if raw.startswith("gid://") else f"gid://shopify/Company/{raw}"

    cursor: str | None = None
    out: list[str] = []
    while True:
        data = graphql_call(shop_domain, access_token, _COMPANY_LOCATIONS_PAGE, {"id": gid, "cursor": cursor})
        node = data.get("company")
        if not isinstance(node, dict):
            return out
        locs = node.get("locations") or {}
        for n in (locs.get("nodes") or []):
            loc_id = n.get("id") if isinstance(n, dict) else None
            if isinstance(loc_id, str) and loc_id:
                out.append(loc_id)
        page = locs.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break
    return out


def _catalog_context_update(
    shop_domain: str,
    access_token: str,
    catalog_id: str,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    """Agrega o quita CompanyLocations de un Catalog en Shopify."""
    data = graphql_call(
        shop_domain,
        access_token,
        _CATALOG_CONTEXT_UPDATE,
        {
            "catalogId": catalog_id,
            "contextsToAdd": {"companyLocationIds": add} if add else None,
            "contextsToRemove": {"companyLocationIds": remove} if remove else None,
        },
    )
    payload = data.get("catalogContextUpdate") or {}
    errs = payload.get("userErrors") or []
    if errs:
        raise ValueError(f"catalogContextUpdate userErrors: {format_user_errors(errs)}")


def _get_catalog_gid(segment: str) -> str | None:
    """Lee el ``catalog_gid`` del segmento desde la BD."""
    with get_session() as session:
        row = session.scalar(
            select(ShopifyPriceSegment).where(ShopifyPriceSegment.segment == segment)
        )
        return row.catalog_gid if row else None


def sync_company_catalog(
    shopify_company_id: str | None,
    old_company_type: str | None,
    new_company_type: str,
) -> str:
    """Mueve las CompanyLocations de una empresa entre catálogos B2B al cambiar de segmento.

    No lanza: los errores se loguean como warnings para no bloquear la actualización de la BD.
    Devuelve el resultado para que el llamador pueda avisarlo:

    - ``"ok"``: la empresa quedó en el catálogo del segmento nuevo.
    - ``"skipped"``: no había nada que mover (sin empresa en Shopify o mismo segmento).
    - ``"no_catalog"``: el segmento nuevo todavía no tiene catálogo en Shopify.
    - ``"error"``: Shopify falló; el CRM quedó actualizado pero el catálogo no.
    """
    if not shopify_company_id:
        return "skipped"
    if old_company_type == new_company_type:
        return "skipped"

    old_segment = COMPANY_TYPE_TO_SEGMENT.get(old_company_type or "")
    new_segment = COMPANY_TYPE_TO_SEGMENT.get(new_company_type)
    if not old_segment and not new_segment:
        return "skipped"

    try:
        shop_domain, access_token = _resolve_shop_and_token()
    except LookupError as e:
        logger.warning("sync_company_catalog: no hay instalación Shopify activa — %s", e)
        return "error"

    try:
        location_gids = _fetch_company_location_gids(shop_domain, access_token, shopify_company_id)
    except Exception as e:
        logger.warning(
            "sync_company_catalog: no se pudieron obtener locations de Shopify — %s",
            e,
            extra={"shopify_company_id": shopify_company_id},
        )
        return "error"

    if not location_gids:
        logger.warning(
            "sync_company_catalog: empresa sin locations en Shopify",
            extra={"shopify_company_id": shopify_company_id},
        )
        return "error"

    resultado = "ok"

    # Quitar del catálogo anterior.
    if old_segment:
        old_catalog = _get_catalog_gid(old_segment)
        if old_catalog:
            try:
                _catalog_context_update(shop_domain, access_token, old_catalog, remove=location_gids)
                logger.info(
                    "sync_company_catalog: locations quitadas del catálogo anterior",
                    extra={"segment": old_segment, "locations": len(location_gids)},
                )
            except Exception as e:
                logger.warning(
                    "sync_company_catalog: no se pudo quitar del catálogo %s — %s",
                    old_segment,
                    e,
                )
                resultado = "error"

    # Agregar al catálogo nuevo.
    if new_segment:
        new_catalog = _get_catalog_gid(new_segment)
        if new_catalog:
            try:
                _catalog_context_update(shop_domain, access_token, new_catalog, add=location_gids)
                logger.info(
                    "sync_company_catalog: locations agregadas al catálogo nuevo",
                    extra={"segment": new_segment, "locations": len(location_gids)},
                )
            except Exception as e:
                logger.warning(
                    "sync_company_catalog: no se pudo agregar al catálogo %s — %s",
                    new_segment,
                    e,
                )
                resultado = "error"
        else:
            logger.warning(
                "sync_company_catalog: el catálogo de %s no existe todavía en BD "
                "(subir un Excel para crearlo o usar rebuild-catalog)",
                new_segment,
                extra={"shopify_company_id": shopify_company_id},
            )
            return "no_catalog"
    return resultado
