#!/usr/bin/env python3
"""Backfill: crea filas en `company_addresses` espejo de las CompanyLocations en Shopify.

Para cada empresa con `shopify_company_id` en la BD se compara la lista local
de direcciones contra las CompanyLocations en Shopify. Por cada location en
Shopify que no exista todavía en la BD (matching por `shopify_location_id`) se
inserta una `CompanyAddress`. La primera location de cada empresa que aún no
tenga una sucursal marcada como `is_primary` se inserta como principal.

Uso:
  python3 scripts/backfill_company_addresses.py                # procesa todo
  python3 scripts/backfill_company_addresses.py --dry-run      # solo reporta
  python3 scripts/backfill_company_addresses.py --company-id <shopify_company_id>

Variables de entorno (desde .env raíz o shell):
  DATABASE_URL        — PostgreSQL (obligatoria)
  SHOPIFY_SHOP        — dominio .myshopify.com (opcional si hay una sola instalación)
  SHOPIFY_API_VERSION — default 2026-04
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent

sys.path.insert(0, str(_SCRIPTS))
from dotenv_loader import load_root_dotenv_defaults  # noqa: E402

load_root_dotenv_defaults(_ROOT)

_SERVICE = _ROOT / "src" / "services" / "company-registration"
_SHARED = _ROOT / "src" / "shared"
for p in (_SERVICE, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sqlalchemy import select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.company import Company  # noqa: E402
from database.models.company_address import CompanyAddress  # noqa: E402
from utils.shopify_customers import (  # noqa: E402
    ShopifyAPIError,
    resolve_shop_and_token,
)

# Reutilizamos el cliente GraphQL local de `shopify_customers` vía import indirecto.
from utils.shopify_customers import _graphql_request  # noqa: E402


_QUERY_COMPANY_LOCATIONS = """
query CompanyLocationsForBackfill($id: ID!, $cursor: String) {
  company(id: $id) {
    id
    locations(first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id
        name
        phone
        shippingAddress {
          firstName
          lastName
          address1
          address2
          city
          zoneCode
          countryCode
          zip
          phone
        }
      }
    }
  }
}
"""


def _normalize_company_gid(raw: str) -> str:
    s = raw.strip()
    if s.startswith("gid://shopify/Company/"):
        return s
    return f"gid://shopify/Company/{s}"


def _fetch_locations(shop: str, token: str, company_gid: str) -> list[dict]:
    out: list[dict] = []
    cursor: str | None = None
    while True:
        data = _graphql_request(
            shop, token, _QUERY_COMPANY_LOCATIONS, {"id": company_gid, "cursor": cursor}
        )
        company = data.get("company") or {}
        if not isinstance(company, dict):
            return out
        locs = company.get("locations") or {}
        for node in locs.get("nodes") or []:
            if isinstance(node, dict):
                out.append(node)
        page = locs.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break
    return out


def _build_address(
    *, company_id, location: dict, mark_primary: bool, company_name: str
) -> CompanyAddress:
    addr = location.get("shippingAddress") or {}
    label = (location.get("name") or "").strip() or f"{company_name} — Principal"
    address1 = str(addr.get("address1") or "").strip() or "—"
    city = str(addr.get("city") or "").strip() or "—"
    country = str(addr.get("countryCode") or "CL").strip().upper()[:2] or "CL"

    return CompanyAddress(
        company_id=company_id,
        label=label,
        is_primary=mark_primary,
        shopify_location_id=str(location.get("id") or "").strip() or None,
        address1=address1,
        address2=(addr.get("address2") or None),
        city=city,
        zone_code=(addr.get("zoneCode") or None),
        country_code=country,
        zip=(addr.get("zip") or None),
        phone=(addr.get("phone") or location.get("phone") or None),
        first_name=(addr.get("firstName") or None),
        last_name=(addr.get("lastName") or None),
    )


def _load_companies(shopify_id_filter: str | None) -> list[Company]:
    with get_session() as session:
        stmt = (
            select(Company)
            .where(Company.shopify_company_id.is_not(None))
            .where(Company.is_system.is_(False))
            .order_by(Company.created_at.asc())
        )
        if shopify_id_filter:
            normalized = shopify_id_filter.strip()
            numeric = (
                normalized.split("/")[-1]
                if normalized.startswith("gid://")
                else normalized
            )
            stmt = stmt.where(Company.shopify_company_id == numeric)
        companies = list(session.scalars(stmt))
        for c in companies:
            session.expunge(c)
    return companies


def _existing_address_state(company_id) -> tuple[set[str], bool]:
    """Devuelve (set de shopify_location_id ya persistidos, hay_alguna_principal)."""
    with get_session() as session:
        rows = list(
            session.scalars(
                select(CompanyAddress).where(CompanyAddress.company_id == company_id)
            )
        )
        gids = {r.shopify_location_id for r in rows if r.shopify_location_id}
        has_primary = any(r.is_primary for r in rows)
    return gids, has_primary


def run(*, dry_run: bool, shopify_id_filter: str | None) -> int:
    try:
        shop, token = resolve_shop_and_token(None)
    except LookupError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    print(f"Tienda: {shop}")
    print(f"Modo:   {'DRY-RUN (sin cambios)' if dry_run else 'REAL (va a escribir en BD)'}")
    print()

    companies = _load_companies(shopify_id_filter)
    print(f"Empresas en BD con shopify_company_id: {len(companies)}")
    print()

    sin_locations: list[str] = []
    creadas_total = 0
    ya_alineadas: list[str] = []
    errores: list[tuple[str, str]] = []
    detalle_creadas: list[tuple[str, int]] = []

    for company in companies:
        label = f"{company.name} ({company.shopify_company_id})"
        company_gid = _normalize_company_gid(company.shopify_company_id)

        try:
            locations = _fetch_locations(shop, token, company_gid)
        except ShopifyAPIError as e:
            print(f"  [ERROR] {label} — {e}", file=sys.stderr)
            errores.append((label, str(e)))
            continue

        if not locations:
            print(f"  [SKIP] {label} — sin CompanyLocations en Shopify")
            sin_locations.append(label)
            continue

        existing_gids, has_primary = _existing_address_state(company.id)
        missing = [
            loc
            for loc in locations
            if isinstance(loc.get("id"), str) and loc["id"] not in existing_gids
        ]
        if not missing:
            print(f"  [OK]   {label} — sin cambios ({len(locations)} location(s) ya alineadas)")
            ya_alineadas.append(label)
            continue

        print(
            f"  [FIX]  {label} — {len(missing)} location(s) a crear "
            f"(total Shopify: {len(locations)}, ya en BD: {len(existing_gids)})"
        )
        for loc in missing:
            print(f"         · {loc.get('id')} — {loc.get('name')}")

        if dry_run:
            creadas_total += len(missing)
            detalle_creadas.append((label, len(missing)))
            continue

        try:
            with get_session() as session:
                local_has_primary = has_primary
                for loc in missing:
                    mark_primary = not local_has_primary
                    addr = _build_address(
                        company_id=company.id,
                        location=loc,
                        mark_primary=mark_primary,
                        company_name=company.name,
                    )
                    session.add(addr)
                    if mark_primary:
                        local_has_primary = True
                session.commit()
            creadas_total += len(missing)
            detalle_creadas.append((label, len(missing)))
            print(f"         ✓ {len(missing)} fila(s) insertadas")
        except Exception as e:
            print(f"         ✗ {e}", file=sys.stderr)
            errores.append((label, str(e)))

    # Resumen
    print()
    print("=" * 60)
    print("Resumen:")
    print(f"  Empresas procesadas:      {len(companies)}")
    print(f"  Sin locations en Shopify: {len(sin_locations)}")
    print(f"  Ya alineadas:             {len(ya_alineadas)}")
    print(
        f"  Filas {'a crear' if dry_run else 'creadas'}: {creadas_total} "
        f"({len(detalle_creadas)} empresa[s])"
    )
    if errores:
        print(f"  Errores: {len(errores)}")
        for lbl, msg in errores:
            print(f"    · {lbl}: {msg}")

    return 0 if not errores else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="No modifica la BD; solo reporta qué se insertaría.",
    )
    parser.add_argument(
        "--company-id",
        default=None,
        help="Procesar solo la empresa con este shopify_company_id (numérico o GID).",
    )
    args = parser.parse_args()
    return run(dry_run=args.dry_run, shopify_id_filter=args.company_id)


if __name__ == "__main__":
    sys.exit(main())
