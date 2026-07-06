"""Helpers Shopify para mapear ``Company`` → ``CompanyLocation`` GIDs.

Compartido entre servicios (prices arma catálogos B2B; quotes resuelve el
``purchasingEntity.companyLocationId`` del Draft Order). A partir de los
``shopify_company_id`` (numéricos o GID) resuelve los ``CompanyLocation`` GIDs.
"""
from __future__ import annotations

from typing import Iterable

from aws_lambda_powertools import Logger

from shopify.graphql import graphql_call

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

    seen_loc: set[str] = set()
    unique: list[str] = []
    for gid in location_gids:
        if gid in seen_loc:
            continue
        seen_loc.add(gid)
        unique.append(gid)
    return unique, missing


def first_location_for_company(
    shop_domain: str, access_token: str, shopify_company_id: str
) -> str | None:
    """GID de la primera ``CompanyLocation`` de una company (o ``None``).

    Útil para el ``purchasingEntity`` del Draft Order cuando la cotización no
    especifica location explícita: se usa la matriz/primera sucursal.
    """
    cgid = normalize_company_gid(shopify_company_id)
    if not cgid:
        return None
    locs = fetch_locations_for_company(shop_domain, access_token, cgid)
    return locs[0] if locs else None


_COMPANY_MAIN_CONTACT = """
query contactoEmpresa($id: ID!) {
  company(id: $id) {
    id
    mainContact { id }
    contacts(first: 1) { nodes { id } }
  }
}
"""


def main_contact_for_company(
    shop_domain: str, access_token: str, shopify_company_id: str
) -> str | None:
    """GID del ``CompanyContact`` principal de la company (o el primero).

    Shopify exige ``companyContactId`` en ``purchasingCompany`` del Draft Order.
    """
    cgid = normalize_company_gid(shopify_company_id)
    if not cgid:
        return None
    data = graphql_call(
        shop_domain, access_token, _COMPANY_MAIN_CONTACT, {"id": cgid}
    )
    node = data.get("company")
    if not isinstance(node, dict):
        return None
    main = node.get("mainContact")
    if isinstance(main, dict) and main.get("id"):
        return main["id"]
    contacts = (node.get("contacts") or {}).get("nodes") or []
    for c in contacts:
        if isinstance(c, dict) and c.get("id"):
            return c["id"]
    return None
