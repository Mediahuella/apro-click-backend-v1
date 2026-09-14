#!/usr/bin/env python3
"""Backfill: `Company.payment_type` según los créditos del DMS de APRO.

APRO es la fuente de verdad del crédito: para cada empresa con RUT de
facturación se consulta `GET /credits/client/{rut}` y se ajusta
`payment_type` (`CREDIT` / `DIRECT`) y el metafield `aproclick.credito` de la
Company B2B, que es lo que decide si el crédito se ve en el checkout.

Es la versión a mano del job diario `companies/handlers/credit_sync.py`: sirve
para la primera carga, para verificar antes de habilitar el job, o para forzar
una pasada después de que APRO otorgue créditos en lote.

Las empresas cuyo crédito no se pueda determinar (DMS caído, sin configurar)
se dejan como están y se reportan aparte: apagar el crédito por un timeout
dejaría a un cliente sin su medio de pago.

Uso:
  python3 scripts/backfill_company_credit_from_dms.py --dry-run   # reporta, no escribe
  python3 scripts/backfill_company_credit_from_dms.py             # aplica
  python3 scripts/backfill_company_credit_from_dms.py --rut 76486379-8

Variables de entorno (desde .env raíz o shell):
  DATABASE_URL              — PostgreSQL (obligatoria)
  APRO_CREDIT_BASE_URL      — host del servicio de créditos
  APRO_CREDIT_BEARER_TOKEN  — token Bearer
  APRO_CREDIT_FORCE_RUTS    — RUTs que siempre se consideran con crédito
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

_SERVICE = _ROOT / "src" / "services" / "companies"
_SHARED = _ROOT / "src" / "shared"
for p in (_SERVICE, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from apro.credit_client import esta_configurado  # noqa: E402
from database.engine import get_session  # noqa: E402
from database.models.company import Company  # noqa: E402
from services.credit_sync_service import consultar_en_paralelo  # noqa: E402
from sqlalchemy import select  # noqa: E402
from utils.shopify_catalog_sync import _resolve_shop_and_token  # noqa: E402
from utils.shopify_credit_sync import sync_company_credit_metafield  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no escribe nada")
    ap.add_argument("--rut", help="sincroniza solo esta empresa")
    args = ap.parse_args()

    if not esta_configurado():
        print(
            "error: falta APRO_CREDIT_BASE_URL o APRO_CREDIT_BEARER_TOKEN",
            file=sys.stderr,
        )
        return 1

    with get_session() as session:
        q = select(Company).where(
            Company.billing_rut.is_not(None), Company.is_system.is_(False)
        )
        if args.rut:
            q = q.where(Company.billing_rut == args.rut.strip())
        empresas = list(session.scalars(q.order_by(Company.name)).all())
        print(f"Empresas con RUT: {len(empresas)}")

        # Las consultas van en paralelo: casi mil empresas, un request cada una.
        print("Consultando créditos en APRO…")
        destinos = consultar_en_paralelo(
            sorted({(c.billing_rut or "").strip() for c in empresas if c.billing_rut})
        )

        if args.dry_run:
            cambios = indeterminadas = 0
            for c in empresas:
                destino = destinos.get((c.billing_rut or "").strip())
                actual = (c.payment_type or "").upper()
                if destino is None:
                    indeterminadas += 1
                    print(f"  [?] {c.name} ({c.billing_rut}): sin respuesta del DMS")
                elif destino != actual:
                    cambios += 1
                    print(f"  [~] {c.name} ({c.billing_rut}): {actual} → {destino}")
            print(f"\nCambiarían: {cambios} · indeterminadas: {indeterminadas}")
            return 0

        try:
            shop_domain, access_token = _resolve_shop_and_token()
        except LookupError:
            print("aviso: sin instalación Shopify activa, solo se actualiza la BD")
            shop_domain, access_token = "", ""

        cambios = indeterminadas = 0
        for c in empresas:
            destino = destinos.get((c.billing_rut or "").strip())
            actual = (c.payment_type or "").upper()
            if destino is None:
                indeterminadas += 1
                continue
            if destino == actual:
                continue
            c.payment_type = destino
            sync_company_credit_metafield(
                c.shopify_company_id,
                destino,
                shop_domain=shop_domain,
                access_token=access_token,
            )
            cambios += 1
            print(f"  {c.name} ({c.billing_rut}): {actual} → {destino}")
        session.commit()
        print(f"\nActualizadas: {cambios} · indeterminadas: {indeterminadas}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
