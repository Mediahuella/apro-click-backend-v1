#!/usr/bin/env python3
"""Fix de companies rotas en Shopify B2B.

Cubre dos clases de problemas detectados en producción:

  Bug 2 — Sin mainContact (--mode maincontact, default):
    Empresas en nuestra BD cuyo company en Shopify no tiene mainContact asignado.
    El cliente existe como ContactCompany pero no puede comprar porque no es mainContact.
    Pasos: asignar customer como contacto si no lo era, luego promoverlo a mainContact.

  Bug 1 — Companies huérfanas (--mode orphans):
    Companies creadas en Shopify por una race condition: la Lambda creó la company en
    Shopify pero no pudo guardarla en nuestra BD (la otra Lambda ganó la carrera).
    Identificadas por tener un `externalId` que apunta a una solicitud aprobada PERO
    cuyo shopify_company_id apunta a una company DISTINTA.
    Acción: reportar (--dry-run) o eliminar (sin --dry-run).

Uso:
  python3 scripts/fix_shopify_companies.py                         # maincontact, dry-run
  python3 scripts/fix_shopify_companies.py --dry-run --mode all    # reporte completo
  python3 scripts/fix_shopify_companies.py --mode maincontact      # solo fix Bug 2 (real)
  python3 scripts/fix_shopify_companies.py --mode orphans          # solo fix Bug 1 (real)
  python3 scripts/fix_shopify_companies.py --mode all              # ambos bugs (real)
  python3 scripts/fix_shopify_companies.py --company-id 123456     # solo esta empresa

Variables de entorno (desde .env o shell):
  DATABASE_URL        — PostgreSQL (obligatoria)
  SHOPIFY_SHOP        — dominio .myshopify.com (opcional si hay una sola instalación)
  SHOPIFY_API_VERSION — default 2026-04
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

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
from database.models.registration_request import CompanyRegistrationRequest  # noqa: E402
from utils.shopify_customers import (  # noqa: E402
    ShopifyAPIError,
    _find_contact_gid_by_customer,
    _graphql_request,
    assign_contact_ordering_role,
    assign_customer_as_contact,
    get_company_contact_info,
    get_company_contacts_with_roles,
    resolve_shop_and_token,
    set_main_contact,
)

# ---------------------------------------------------------------------------
# GraphQL adicionales
# ---------------------------------------------------------------------------

_QUERY_LIST_COMPANIES = """
query ListCompanies($first: Int!, $after: String) {
  companies(first: $first, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      node {
        id
        name
        externalId
        mainContact {
          id
          customer {
            id
            defaultEmailAddress { emailAddress }
          }
        }
        contacts(first: 10) {
          nodes {
            id
            customer {
              id
              defaultEmailAddress { emailAddress }
            }
          }
        }
      }
    }
  }
}
"""

_MUTATION_COMPANY_DELETE = """
mutation CompanyDelete($id: ID!) {
  companyDelete(id: $id) {
    deletedCompanyId
    userErrors { field message code }
  }
}
"""


# ---------------------------------------------------------------------------
# Helpers Shopify
# ---------------------------------------------------------------------------

def _normalize_company_gid(raw: str) -> str:
    s = str(raw).strip()
    if s.startswith("gid://shopify/Company/"):
        return s
    return f"gid://shopify/Company/{s}"


def _list_all_shopify_companies(shop: str, token: str) -> list[dict[str, Any]]:
    """Descarga TODAS las companies de Shopify paginando de 50 en 50."""
    all_nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    page = 0
    while True:
        page += 1
        vars_: dict[str, Any] = {"first": 50}
        if cursor:
            vars_["after"] = cursor
        data = _graphql_request(shop, token, _QUERY_LIST_COMPANIES, vars_)
        conn = data.get("companies") or {}
        edges = conn.get("edges") or []
        for edge in edges:
            node = edge.get("node")
            if isinstance(node, dict):
                all_nodes.append(node)
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        print(f"    Shopify: página {page} → {len(all_nodes)} companies descargadas")
    return all_nodes


def _delete_shopify_company(shop: str, token: str, company_gid: str) -> None:
    """Elimina una company en Shopify. Lanza ShopifyAPIError si falla."""
    data = _graphql_request(shop, token, _MUTATION_COMPANY_DELETE, {"id": company_gid})
    payload = data.get("companyDelete") or {}
    errs = payload.get("userErrors") or []
    if errs:
        raise ShopifyAPIError(422, {"userErrors": errs, "message": "companyDelete"})


# ---------------------------------------------------------------------------
# Carga desde BD
# ---------------------------------------------------------------------------

def _load_companies_from_db(
    shopify_id_filter: str | None,
) -> list[tuple[Company, list[Client]]]:
    """Carga empresas con shopify_company_id y sus clientes desde la BD."""
    with get_session() as session:
        stmt = (
            select(Company)
            .where(Company.shopify_company_id.is_not(None))
            .where(Company.is_system.is_(False))
            .order_by(Company.created_at.asc())
        )
        if shopify_id_filter:
            numeric = (
                shopify_id_filter.strip().split("/")[-1]
                if shopify_id_filter.startswith("gid://")
                else shopify_id_filter.strip()
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


def _load_approved_requests() -> list[CompanyRegistrationRequest]:
    """Carga todas las solicitudes APPROVED con resolved_company_id."""
    with get_session() as session:
        rows = list(
            session.scalars(
                select(CompanyRegistrationRequest)
                .where(CompanyRegistrationRequest.status == "APPROVED")
                .where(CompanyRegistrationRequest.resolved_company_id.is_not(None))
                .order_by(CompanyRegistrationRequest.created_at.asc())
            )
        )
    return rows


def _get_company_shopify_id(company_id) -> str | None:
    with get_session() as session:
        c = session.get(Company, company_id)
        return c.shopify_company_id if c else None


# ---------------------------------------------------------------------------
# Bug 2: Fix mainContact
# ---------------------------------------------------------------------------

def fix_main_contacts(
    shop: str,
    token: str,
    *,
    dry_run: bool,
    shopify_id_filter: str | None,
) -> dict[str, int]:
    print("=" * 60)
    print("BUG 2 — Empresas sin mainContact en Shopify")
    print("=" * 60)
    rows = _load_companies_from_db(shopify_id_filter)
    print(f"Empresas en BD con shopify_company_id: {len(rows)}")
    print()

    ya_ok = 0
    corregidas = 0
    sin_cliente_bd = 0
    errores = 0

    for company, clients in rows:
        company_gid = _normalize_company_gid(company.shopify_company_id)
        label = f"{company.name} (shopify_id={company.shopify_company_id})"

        if not clients:
            print(f"  [SKIP] {label} — sin cliente en BD con shopify_customer_id")
            sin_cliente_bd += 1
            continue

        try:
            info = get_company_contact_info(shop, token, company_gid)
        except ShopifyAPIError as e:
            print(f"  [ERR]  {label} — Shopify error: {e}")
            errores += 1
            continue

        if info is None:
            print(f"  [SKIP] {label} — no encontrada en Shopify (¿borrada?)")
            sin_cliente_bd += 1
            continue

        if info.get("mainContact"):
            cust = info["mainContact"].get("customer") or {}
            email = (cust.get("defaultEmailAddress") or {}).get("emailAddress") or cust.get("id")
            print(f"  [OK]   {label} — mainContact ya asignado ({email})")
            ya_ok += 1
            continue

        client = clients[0]
        customer_numeric = client.shopify_customer_id
        customer_gid_target = f"gid://shopify/Customer/{customer_numeric}"

        # ¿Ya es contacto pero sin ser mainContact?
        contact_gid = _find_contact_gid_by_customer(
            info.get("contacts") or [], customer_numeric
        )

        if contact_gid:
            print(
                f"  [FIX]  {label}\n"
                f"         Cliente {client.email} YA es contacto pero NO es mainContact\n"
                f"         → promoviendo a mainContact"
            )
            if not dry_run:
                try:
                    set_main_contact(shop, token, company_gid, contact_gid)
                    print("         ✓ companyAssignMainContact OK")
                    corregidas += 1
                except ShopifyAPIError as e:
                    print(f"         ✗ {e}")
                    errores += 1
            else:
                print("         [DRY-RUN] se omitiría companyAssignMainContact")
                corregidas += 1
        else:
            print(
                f"  [FIX]  {label}\n"
                f"         Cliente {client.email} NO es contacto\n"
                f"         → assign como contacto + promover a mainContact"
            )
            if not dry_run:
                try:
                    # 1. Asignar como contacto
                    from utils.shopify_customers import _graphql_request, MUTATION_COMPANY_ASSIGN_CUSTOMER, _to_customer_gid, _assign_user_errors
                    assign_data = _graphql_request(
                        shop, token,
                        MUTATION_COMPANY_ASSIGN_CUSTOMER,
                        {"companyId": company_gid, "customerId": _to_customer_gid(customer_numeric)},
                    )
                    aerrs = _assign_user_errors(assign_data)
                    if aerrs:
                        raise ShopifyAPIError(422, {"userErrors": aerrs})
                    new_contact_gid = (
                        (assign_data.get("companyAssignCustomerAsContact") or {})
                        .get("companyContact", {})
                        .get("id")
                    )
                    if not new_contact_gid:
                        # Fallback: buscar en la company después del assign
                        info2 = get_company_contact_info(shop, token, company_gid)
                        if info2:
                            new_contact_gid = _find_contact_gid_by_customer(
                                info2.get("contacts") or [], customer_numeric
                            )
                    if not new_contact_gid:
                        raise ShopifyAPIError(
                            422,
                            {"message": "assign OK pero no se pudo obtener contact_gid"}
                        )
                    print("         ✓ companyAssignCustomerAsContact OK")
                    # 2. Promover a mainContact
                    set_main_contact(shop, token, company_gid, new_contact_gid)
                    print("         ✓ companyAssignMainContact OK")
                    corregidas += 1
                except ShopifyAPIError as e:
                    print(f"         ✗ {e}")
                    errores += 1
            else:
                print("         [DRY-RUN] se omitiría assign + companyAssignMainContact")
                corregidas += 1

    print()
    print(f"  Ya OK:             {ya_ok}")
    print(f"  {'Corregidas' if not dry_run else 'Requieren corrección'}: {corregidas}")
    print(f"  Sin cliente en BD: {sin_cliente_bd}")
    print(f"  Errores:           {errores}")
    return {"ok": ya_ok, "fixed": corregidas, "skipped": sin_cliente_bd, "errors": errores}


# ---------------------------------------------------------------------------
# Bug 1: Companies huérfanas
# ---------------------------------------------------------------------------

def fix_orphan_companies(
    shop: str,
    token: str,
    *,
    dry_run: bool,
) -> dict[str, int]:
    print()
    print("=" * 60)
    print("BUG 1 — Companies huérfanas en Shopify")
    print("=" * 60)
    print("Descargando todas las companies de Shopify...")

    try:
        shopify_companies = _list_all_shopify_companies(shop, token)
    except ShopifyAPIError as e:
        print(f"[ERROR] No se pudo obtener la lista de Shopify: {e}")
        return {"orphans": 0, "errors": 1}

    print(f"Total companies en Shopify: {len(shopify_companies)}")

    # Todos los shopify_company_id que SÍ están en nuestra BD
    with get_session() as session:
        all_our_shopify_ids: set[str] = set(
            r[0] for r in session.execute(
                select(Company.shopify_company_id)
                .where(Company.shopify_company_id.is_not(None))
            ).all()
        )

    print(f"Companies en nuestra BD:    {len(all_our_shopify_ids)}")
    print()

    # Clasificar companies de Shopify
    en_bd: list[dict] = []
    sin_bd_con_main: list[dict] = []   # no están en BD pero tienen mainContact → revisar manualmente
    huerfanas: list[dict] = []         # no están en BD Y sin mainContact → eliminar

    for node in shopify_companies:
        shopify_gid: str = node.get("id") or ""
        shopify_numeric = shopify_gid.split("/")[-1] if "/" in shopify_gid else shopify_gid
        name: str = node.get("name") or ""
        external_id: str | None = node.get("externalId")
        has_main_contact = node.get("mainContact") is not None
        main_email = ""
        if has_main_contact:
            cust = (node["mainContact"].get("customer") or {})
            main_email = (cust.get("defaultEmailAddress") or {}).get("emailAddress") or cust.get("id") or ""

        entry = {
            "shopify_gid": shopify_gid,
            "shopify_numeric": shopify_numeric,
            "name": name,
            "external_id": external_id,
            "has_main_contact": has_main_contact,
            "main_email": main_email,
        }

        if shopify_numeric in all_our_shopify_ids:
            en_bd.append(entry)
        elif has_main_contact:
            # Tiene mainContact pero no está en nuestra BD → probablemente creada fuera del sistema
            sin_bd_con_main.append(entry)
        else:
            # Sin BD y sin mainContact → huérfana de la race condition
            huerfanas.append(entry)

    print(f"  En nuestra BD:                    {len(en_bd)}")
    print(f"  Sin BD + con mainContact (manual): {len(sin_bd_con_main)}")
    print(f"  Sin BD + sin mainContact (huérfanas): {len(huerfanas)}")

    if sin_bd_con_main:
        print()
        print("  Companies fuera de BD con mainContact (NO se tocan — revisar manualmente):")
        for h in sin_bd_con_main:
            print(
                f"    · {h['name']} | shopify_id={h['shopify_numeric']}"
                f" | externalId={h['external_id']} | mainContact={h['main_email']}"
            )

    if not huerfanas:
        print()
        print("  [OK] No se encontraron companies huérfanas sin mainContact.")
        return {"orphans": 0, "errors": 0}

    print()
    print(f"Companies huérfanas a {'reportar' if dry_run else 'ELIMINAR'}: {len(huerfanas)}")
    print()

    eliminadas = 0
    errores = 0

    for h in huerfanas:
        print(
            f"  [HUÉRFANA] {h['name']}\n"
            f"    shopify_id:  {h['shopify_numeric']}\n"
            f"    externalId:  {h['external_id'] or '(ninguno)'}\n"
            f"    mainContact: NO"
        )
        if dry_run:
            print("    [DRY-RUN] se omitiría companyDelete")
            eliminadas += 1
        else:
            try:
                _delete_shopify_company(shop, token, h["shopify_gid"])
                print("    ✓ companyDelete OK")
                eliminadas += 1
            except ShopifyAPIError as e:
                print(f"    ✗ {e}")
                errores += 1

    print()
    print(f"  {'Eliminadas' if not dry_run else 'Para eliminar'}: {eliminadas}")
    print(f"  Errores: {errores}")
    return {"orphans": eliminadas, "errors": errores}


# ---------------------------------------------------------------------------
# Bug 3: Contactos sin rol de location (no pueden comprar aunque tengan mainContact)
# ---------------------------------------------------------------------------

def fix_location_roles(
    shop: str,
    token: str,
    *,
    dry_run: bool,
    shopify_id_filter: str | None,
) -> dict[str, int]:
    print()
    print("=" * 60)
    print("ROLES — Contactos sin rol de compra en CompanyLocation")
    print("=" * 60)
    rows = _load_companies_from_db(shopify_id_filter)
    print(f"Empresas en BD con shopify_company_id: {len(rows)}")
    print()

    ya_ok = 0
    corregidas = 0
    sin_datos = 0
    errores = 0

    for company, clients in rows:
        company_gid = _normalize_company_gid(company.shopify_company_id)
        label = f"{company.name} (shopify_id={company.shopify_company_id})"

        if not clients:
            sin_datos += 1
            continue

        client = clients[0]
        customer_numeric = client.shopify_customer_id
        customer_gid_target = f"gid://shopify/Customer/{customer_numeric}"

        try:
            info = get_company_contacts_with_roles(shop, token, company_gid)
        except ShopifyAPIError as e:
            print(f"  [ERR]  {label} — {e}")
            errores += 1
            continue

        if info is None:
            print(f"  [SKIP] {label} — no encontrada en Shopify")
            sin_datos += 1
            continue

        locations = info.get("locations") or []
        contacts = info.get("contacts") or []
        location_gids = [loc["id"] for loc in locations if loc.get("id")]

        # Buscar el contacto que corresponde a nuestro cliente
        contact_node = None
        for c in contacts:
            cust_id = ((c.get("customer") or {}).get("id") or "").strip()
            if cust_id == customer_gid_target:
                contact_node = c
                break

        if contact_node is None:
            print(f"  [SKIP] {label} — cliente {client.email} no es contacto en Shopify")
            sin_datos += 1
            continue

        contact_gid = contact_node.get("id")
        role_assignments = [
            edge.get("node")
            for edge in (contact_node.get("roleAssignments") or {}).get("edges", [])
            if edge.get("node")
        ]

        if role_assignments:
            roles_str = ", ".join(
                f"{r.get('role', {}).get('name')} @ {r.get('companyLocation', {}).get('name')}"
                for r in role_assignments
            )
            print(f"  [OK]   {label} — {client.email} ya tiene rol: {roles_str}")
            ya_ok += 1
            continue

        # Sin roles → no puede comprar
        locs_str = ", ".join(loc.get("name", loc.get("id", "?")) for loc in locations)
        print(
            f"  [FIX]  {label}\n"
            f"         {client.email} sin rol en locations: {locs_str or '(ninguna)'}"
        )

        if not location_gids:
            print("         [WARN] No hay locations en Shopify para asignar rol")
            sin_datos += 1
            continue

        # Obtener el GID del rol ORDERING_ONLY de ESTA company (los roles son por-company)
        contact_roles = info.get("contactRoles") or []
        role_gid: str | None = None
        for cr in contact_roles:
            name = (cr.get("name") or "").lower()
            if "ordering" in name:
                role_gid = cr.get("id")
                break
        if not role_gid:
            for cr in contact_roles:
                name = (cr.get("name") or "").lower()
                if "location" in name:
                    role_gid = cr.get("id")
                    break

        if not role_gid:
            print(
                f"         [WARN] No se encontraron roles disponibles (contactRoles) en Shopify "
                f"para {label}. La company puede necesitar ser recreada en Shopify."
            )
            errores += 1
            continue

        if dry_run:
            role_name = next(
                (cr.get("name") for cr in contact_roles if cr.get("id") == role_gid), role_gid
            )
            print(f"         [DRY-RUN] se omitiría companyContactAssignRole (rol: {role_name})")
            corregidas += 1
        else:
            try:
                assign_contact_ordering_role(
                    shop, token, contact_gid, location_gids, role_gid=role_gid
                )
                print(f"         ✓ ORDERING_ONLY asignado en {len(location_gids)} location(s)")
                corregidas += 1
            except Exception as e:
                print(f"         ✗ {e}")
                errores += 1

    print()
    print(f"  Ya tenían rol:                  {ya_ok}")
    print(f"  {'Corregidos' if not dry_run else 'Requieren corrección'}: {corregidas}")
    print(f"  Sin datos (skip):               {sin_datos}")
    print(f"  Errores:                        {errores}")
    return {"ok": ya_ok, "fixed": corregidas, "skipped": sin_datos, "errors": errores}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fix de companies rotas en Shopify B2B (Bug 1 y Bug 2).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Solo reporta, sin modificar Shopify (DEFAULT). Desactivar con --no-dry-run.",
    )
    parser.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        help="Ejecuta los cambios reales en Shopify.",
    )
    parser.add_argument(
        "--mode",
        choices=["maincontact", "orphans", "roles", "all"],
        default="roles",
        help=(
            "roles: fix contactos sin rol de compra en location (default). "
            "maincontact: fix companies sin mainContact. "
            "orphans: eliminar companies huérfanas. "
            "all: los tres."
        ),
    )
    parser.add_argument(
        "--company-id",
        dest="company_id",
        default=None,
        metavar="SHOPIFY_COMPANY_ID",
        help="(Solo modo maincontact) Procesa solo esta empresa (ID numérico o GID).",
    )
    args = parser.parse_args()

    print()
    print("fix_shopify_companies.py")
    print(f"Modo:    {args.mode}")
    print(f"Acción:  {'DRY-RUN (sin cambios)' if args.dry_run else '⚠️  REAL — va a modificar Shopify'}")
    print()

    try:
        shop, token = resolve_shop_and_token(None)
    except LookupError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    print(f"Tienda: {shop}")
    print()

    total_errors = 0

    if args.mode in ("maincontact", "all"):
        result = fix_main_contacts(
            shop, token,
            dry_run=args.dry_run,
            shopify_id_filter=args.company_id,
        )
        total_errors += result["errors"]

    if args.mode in ("roles", "all"):
        result = fix_location_roles(
            shop, token,
            dry_run=args.dry_run,
            shopify_id_filter=args.company_id,
        )
        total_errors += result["errors"]

    if args.mode in ("orphans", "all"):
        if args.company_id:
            print("[WARN] --company-id se ignora en modo 'orphans'")
        result = fix_orphan_companies(shop, token, dry_run=args.dry_run)
        total_errors += result["errors"]

    print()
    print("Completado." + (" Con errores." if total_errors else " Sin errores."))
    return 1 if total_errors else 0


if __name__ == "__main__":
    sys.exit(main())
