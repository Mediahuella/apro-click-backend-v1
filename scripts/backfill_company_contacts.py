#!/usr/bin/env python3
"""Backfill: enlaza un CompanyContact a las empresas Shopify B2B que no tienen ninguno.

Para cada empresa en la BD con `shopify_company_id`:
  1. Consulta Shopify si la company ya tiene `mainContact`.
  2. Si no tiene contacto, busca el cliente asociado en la tabla `clients`
     (campo `shopify_customer_id`).
  3. Llama `companyAssignCustomerAsContact` para enlazarlos.

Uso:
  python3 scripts/backfill_company_contacts.py              # procesa todo
  python3 scripts/backfill_company_contacts.py --dry-run    # solo reporta, sin modificar
  python3 scripts/backfill_company_contacts.py --company-id <shopify_company_id>

Variables de entorno (desde .env o shell):
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
from database.models.client import Client  # noqa: E402
from database.models.company import Company  # noqa: E402
from utils.shopify_customers import (  # noqa: E402
    ShopifyAPIError,
    assign_customer_as_contact,
    get_company_contact_info,
    set_main_contact,
    resolve_shop_and_token,
)


def _normalize_company_gid(raw: str) -> str:
    s = raw.strip()
    if s.startswith("gid://shopify/Company/"):
        return s
    return f"gid://shopify/Company/{s}"


def _load_companies_with_clients(
    shopify_id_filter: str | None,
) -> list[tuple[Company, list[Client]]]:
    """Carga empresas con shopify_company_id y sus clients desde la BD."""
    with get_session() as session:
        stmt = (
            select(Company)
            .where(Company.shopify_company_id.is_not(None))
            .where(Company.is_system.is_(False))
            .order_by(Company.created_at.asc())
        )
        if shopify_id_filter:
            normalized = shopify_id_filter.strip()
            # aceptar "123456" o "gid://shopify/Company/123456"
            numeric = (
                normalized.split("/")[-1]
                if normalized.startswith("gid://")
                else normalized
            )
            stmt = stmt.where(Company.shopify_company_id == numeric)

        companies = list(session.scalars(stmt))

        result: list[tuple[Company, list[Client]]] = []
        for company in companies:
            clients = list(
                session.scalars(
                    select(Client)
                    .where(Client.company_id == company.id)
                    .where(Client.shopify_customer_id.is_not(None))
                )
            )
            result.append((company, clients))

    return result


def run(*, dry_run: bool, shopify_id_filter: str | None) -> int:
    try:
        shop, token = resolve_shop_and_token(None)
    except LookupError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    print(f"Tienda: {shop}")
    print(f"Modo:   {'DRY-RUN (sin cambios)' if dry_run else 'REAL (va a modificar Shopify)'}")
    print()

    rows = _load_companies_with_clients(shopify_id_filter)
    print(f"Empresas en BD con shopify_company_id: {len(rows)}")
    print()

    sin_cliente_bd: list[str] = []
    ya_tiene_contacto: list[str] = []
    corregidas: list[str] = []
    errores: list[tuple[str, str]] = []

    for company, clients in rows:
        company_gid = _normalize_company_gid(company.shopify_company_id)
        label = f"{company.name} ({company.shopify_company_id})"

        if not clients:
            print(f"  [SKIP] {label} — sin cliente en BD (tabla clients)")
            sin_cliente_bd.append(label)
            continue

        # Consulta Shopify: mainContact + lista de contactos existentes
        try:
            info = get_company_contact_info(shop, token, company_gid)
        except ShopifyAPIError as e:
            print(f"  [ERROR] {label} — {e}", file=sys.stderr)
            errores.append((label, str(e)))
            continue

        if info is None:
            print(f"  [SKIP] {label} — company no encontrada en Shopify (¿borrada?)")
            sin_cliente_bd.append(label)
            continue

        if info["mainContact"]:
            cust = (info["mainContact"].get("customer") or {})
            email = (cust.get("defaultEmailAddress") or {}).get("emailAddress") or cust.get("id")
            print(f"  [OK]   {label} — ya tiene mainContact ({email})")
            ya_tiene_contacto.append(label)
            continue

        client = clients[0]
        customer_gid_target = f"gid://shopify/Customer/{client.shopify_customer_id}"

        # Buscar si el cliente ya es un CompanyContact (sin ser mainContact)
        existing_contact_gid: str | None = None
        for c in (info["contacts"] or []):
            cust_node = c.get("customer") or {}
            if cust_node.get("id") == customer_gid_target:
                existing_contact_gid = c.get("id")
                break

        if existing_contact_gid:
            print(
                f"  [FIX]  {label} — cliente ya es contacto pero SIN mainContact "
                f"→ promoviendo {client.email} a mainContact"
            )
            if dry_run:
                corregidas.append(label)
                continue
            try:
                set_main_contact(shop, token, company_gid, existing_contact_gid)
                print(f"         ✓ companyAssignMainContact OK")
                corregidas.append(label)
            except ShopifyAPIError as e:
                print(f"         ✗ {e}", file=sys.stderr)
                errores.append((label, str(e)))
        else:
            print(
                f"  [FIX]  {label} — SIN contacto en Shopify "
                f"→ asignando customer {client.shopify_customer_id} ({client.email})"
            )
            if dry_run:
                corregidas.append(label)
                continue
            try:
                assign_customer_as_contact(shop, token, company_gid, client.shopify_customer_id)
                print(f"         ✓ companyAssignCustomerAsContact OK")
                corregidas.append(label)
            except ShopifyAPIError as e:
                print(f"         ✗ {e}", file=sys.stderr)
                errores.append((label, str(e)))

    print()
    print("=" * 60)
    print("Resumen")
    print("=" * 60)
    print(f"  Ya tenían contacto:       {len(ya_tiene_contacto)}")
    print(f"  {'Corregidas' if not dry_run else 'Requieren corrección'}: {len(corregidas)}")
    print(f"  Sin cliente en BD:        {len(sin_cliente_bd)}")
    print(f"  Errores:                  {len(errores)}")

    if errores:
        print()
        print("Detalle de errores:")
        for name, msg in errores:
            print(f"  - {name}: {msg}")

    if sin_cliente_bd:
        print()
        print("Empresas sin cliente en BD (requieren revisión manual):")
        for name in sin_cliente_bd:
            print(f"  - {name}")

    return 1 if errores else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill: asigna CompanyContact a empresas Shopify B2B que no tienen ninguno.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo reporta el estado, sin llamar a Shopify para modificar.",
    )
    parser.add_argument(
        "--company-id",
        dest="company_id",
        default=None,
        metavar="SHOPIFY_COMPANY_ID",
        help="Procesa solo esta empresa (ID numérico o GID completo).",
    )
    args = parser.parse_args()
    return run(dry_run=args.dry_run, shopify_id_filter=args.company_id)


if __name__ == "__main__":
    sys.exit(main())
