#!/usr/bin/env python3
"""Crea en Shopify B2B una empresa que ya existe en el CRM pero sin `shopify_company_id`.

Una empresa creada desde el panel (`POST /api/v1/companies`) solo se escribe en la
base del CRM: ese camino nunca llama a `companyCreate`. La creación en Shopify vive
únicamente en el flujo de aprobación de una solicitud de registro
(`company-registration`). Este script cubre el hueco replicando lo que hace
`RegistrationRequestService.approve_request` después de crear la Company en Shopify:

  1. `ensure_shopify_b2b_company` — Company + CompanyLocation + contacto principal.
  2. `companies.shopify_company_id` en la BD del CRM.
  3. `Client` principal (espejo del Customer de Shopify).
  4. `CompanyAddress` principal (espejo de la CompanyLocation).
  5. Catálogo B2B del segmento correspondiente al `company_type`.

Uso:
  python3 scripts/create_shopify_company_for_crm_company.py <company_uuid> \\
      --contact-email correo@empresa.cl --contact-name "Nombre Apellido" \\
      --contact-phone "+56912345678" \\
      --address1 "Calle 123" --city "COMUNA" --region "Región Metropolitana" \\
      [--zip 0000000] [--apply]

Sin `--apply` es un dry-run: muestra lo que haría y no escribe en Shopify ni en la BD.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "src" / "services" / "company-registration"
for p in (str(SERVICE), str(ROOT / "src" / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

sys.path.insert(0, str(ROOT / "scripts"))
from dotenv_loader import load_root_dotenv_defaults  # noqa: E402

load_root_dotenv_defaults(ROOT)

from database.engine import get_session  # noqa: E402
from database.models.client import Client  # noqa: E402
from database.models.company import Company  # noqa: E402
from database.models.company_address import CompanyAddress  # noqa: E402
from utils.chile_shopify_zone import normalize_chile_shopify_zone_code  # noqa: E402
from utils.shopify_customers import (  # noqa: E402
    ensure_shopify_b2b_company,
    resolve_shop_and_token,
)


def _split_name(full: str) -> tuple[str | None, str | None]:
    parts = [p for p in (full or "").strip().split() if p]
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("company_id", help="UUID de la Company en el CRM")
    ap.add_argument("--contact-email", required=True)
    ap.add_argument("--contact-name", required=True)
    ap.add_argument("--contact-phone", default=None)
    ap.add_argument("--address1", required=True, help="Calle y número de despacho")
    ap.add_argument("--city", required=True, help="Comuna o ciudad")
    ap.add_argument("--region", default=None, help="Región (se traduce a ISO CL-XX)")
    ap.add_argument("--zip", dest="zip_code", default=None)
    ap.add_argument("--shop-domain", default=None)
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Ejecuta de verdad. Sin este flag es dry-run.",
    )
    args = ap.parse_args()

    try:
        cid = uuid.UUID(args.company_id)
    except ValueError:
        print(f"error: company_id no es un UUID: {args.company_id}", file=sys.stderr)
        return 1

    with get_session() as session:
        company = session.get(Company, cid)
        if not company:
            print(f"error: no existe la empresa {cid}", file=sys.stderr)
            return 1
        if company.shopify_company_id:
            print(
                f"error: {company.name} ya tiene shopify_company_id="
                f"{company.shopify_company_id}; nada que hacer.",
                file=sys.stderr,
            )
            return 1
        name = company.name
        company_type = company.company_type

    first_name, last_name = _split_name(args.contact_name)
    zone_code = (
        normalize_chile_shopify_zone_code(args.region) if args.region else None
    )
    shipping = {
        "address1": args.address1.strip(),
        "city": args.city.strip(),
        "country_code": "CL",
        "first_name": first_name,
        "last_name": last_name,
        "phone": args.contact_phone,
    }
    if zone_code:
        shipping["zone_code"] = zone_code
    if args.zip_code:
        shipping["zip"] = args.zip_code

    print(f"Empresa CRM   : {name} ({cid}) — segmento {company_type}")
    print(f"Contacto      : {args.contact_name} <{args.contact_email}> {args.contact_phone or ''}")
    print(f"Despacho      : {shipping['address1']}, {shipping['city']}, {zone_code or '(sin región)'}")

    shop, token = resolve_shop_and_token(args.shop_domain)
    print(f"Tienda        : {shop}")

    if not args.apply:
        print("\n[dry-run] No se creó nada. Repetir con --apply para ejecutar.")
        return 0

    result = ensure_shopify_b2b_company(
        shop,
        token,
        external_id=str(cid),
        company_name=name,
        contact_email=args.contact_email.strip(),
        first_name=first_name,
        last_name=last_name,
        contact_phone=args.contact_phone,
        shipping=shipping,
    )
    shopify_company_id = result["shopify_company_numeric_id"]
    shopify_customer_id = result["shopify_customer_numeric_id"]
    location_gids = result.get("shopify_company_location_gids") or []
    print(f"\nShopify Company: {shopify_company_id}")
    print(f"Shopify Customer: {shopify_customer_id}")
    print(f"CompanyLocations: {location_gids}")

    with get_session() as session:
        company = session.get(Company, cid)
        company.shopify_company_id = shopify_company_id
        session.add(
            Client(
                company_id=company.id,
                shopify_customer_id=shopify_customer_id,
                email=args.contact_email.strip(),
                name=args.contact_name.strip(),
                phone=args.contact_phone,
                is_main_contact=True,
            )
        )
        if location_gids:
            session.add(
                CompanyAddress(
                    company_id=company.id,
                    label=f"{name} — Principal",
                    is_primary=True,
                    shopify_location_id=location_gids[0],
                    address1=shipping["address1"],
                    city=shipping["city"],
                    zone_code=zone_code,
                    country_code="CL",
                    zip=args.zip_code,
                    phone=args.contact_phone,
                    first_name=first_name,
                    last_name=last_name,
                )
            )
        session.commit()
    print("BD del CRM actualizada (shopify_company_id, Client y sucursal principal).")

    # Catálogo B2B del segmento: sin esto la empresa no ve precios B2B.
    from services.registration_request_service import _sync_new_company_to_catalog

    _sync_new_company_to_catalog(
        shop_domain=shop,
        access_token=token,
        company_type=company_type,
        location_gids=location_gids,
    )
    print("Catálogo B2B sincronizado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
