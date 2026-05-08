"""Helpers Shopify para mapear ``Company`` → ``CompanyLocation`` GIDs.

Lo usamos al crear el ``Catalog`` B2B de un segmento: a partir de las
``companies`` de la BD (con ``company_type`` ∈ {SMALL, MEDIUM, BIG}), resolvemos
todos los ``CompanyLocation`` que vamos a asociar al catálogo.

Diseño:

- :func:`fetch_locations_for_companies` recibe los ``shopify_company_id`` (sin
  prefijo gid o con él, normaliza ambos) y devuelve la lista plana de
  ``CompanyLocation`` GIDs.
- Internamente usa la query GraphQL ``nodes(ids: [...])`` paginando ``locations``
  por company para no perder ninguna.

Notas:

- ``shopify_company_id`` en la BD se guarda como número plano (``"3567190200"``)
  o como GID (``"gid://shopify/Company/3567190200"``). Aceptamos ambos.
- Una company puede tener varias locations (ej. una matriz + sucursales). Las
  agregamos todas al catálogo del segmento.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

from aws_lambda_powertools import Logger

service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

lambda_root = "/var/task"
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

from utils.shopify_graphql import graphql_call  # noqa: E402

logger = Logger()


_COMPANY_LOCATIONS_PAGE = """
query CompanyLocs($id: ID!, $cursor: String) {
  company(id: $id) {
    id
    name
    locations(first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { id name }
    }
  }
}
"""


def normalize_company_gid(value: str) -> str:
    """Acepta ``"3567190200"`` o ``"gid://shopify/Company/3567190200"``."""
    s = str(value or "").strip()
    if not s:
        return ""
    if s.startswith("gid://shopify/Company/"):
        return s
    return f"gid://shopify/Company/{s}"


def fetch_locations_for_company(
    shop_domain: str,
    access_token: str,
    company_gid: str,
) -> list[str]:
    """Devuelve todos los ``CompanyLocation`` GIDs de una sola company.

    Devuelve lista vacía si la company no existe en Shopify (caller decide
    si loguear). Lanza si la query falla por otra razón.
    """
    cursor: str | None = None
    out: list[str] = []
    while True:
        data = graphql_call(
            shop_domain,
            access_token,
            _COMPANY_LOCATIONS_PAGE,
            {"id": company_gid, "cursor": cursor},
        )
        node = data.get("company")
        if not isinstance(node, dict):
            return out
        locs = node.get("locations") or {}
        for n in (locs.get("nodes") or []):
            gid = n.get("id") if isinstance(n, dict) else None
            if isinstance(gid, str) and gid:
                out.append(gid)
        page = locs.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break
    return out


def fetch_locations_for_companies(
    shop_domain: str,
    access_token: str,
    shopify_company_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Resuelve un grupo de companies a sus location GIDs.

    Args:
        shop_domain: ``mi-tienda.myshopify.com``.
        access_token: token Admin API.
        shopify_company_ids: Iterable de IDs (numéricos o ya como GID).

    Returns:
        ``(location_gids, missing_company_gids)`` — ``missing_company_gids`` lista
        las companies que Shopify no devolvió (probablemente borradas).
    """
    location_gids: list[str] = []
    missing: list[str] = []

    seen: set[str] = set()
    for raw in shopify_company_ids:
        cgid = normalize_company_gid(raw)
        if not cgid or cgid in seen:
            continue
        seen.add(cgid)
        try:
            locs = fetch_locations_for_company(shop_domain, access_token, cgid)
        except Exception:
            logger.exception(
                "Error consultando locations de company en Shopify",
                extra={"company_gid": cgid},
            )
            missing.append(cgid)
            continue
        if not locs:
            missing.append(cgid)
            continue
        location_gids.extend(locs)

    # Dedupe locations (alguna company podría compartir, edge case raro).
    seen_loc: set[str] = set()
    unique: list[str] = []
    for gid in location_gids:
        if gid in seen_loc:
            continue
        seen_loc.add(gid)
        unique.append(gid)
    return unique, missing
