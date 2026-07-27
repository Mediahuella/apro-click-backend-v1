#!/usr/bin/env python3
"""Backfill: reconcilia `QuoteRequest.apro_ia_status` con APRO IA (DMS externo).

`_sync_apro_ia_and_pdf` no guardaba el `status` que devuelve APRO IA al crear
o editar una cotización (`POST`/`PATCH /quotation/aproclick`) — solo se
actualizaba vía el webhook entrante. Este script reconcilia las cotizaciones
ya procesadas (con `apro_ia_folio` asignado) consultando su estado actual
con `GET /quotation/aproclick/{folio}`.

Uso:
  python3 scripts/backfill_apro_ia_quote_status.py              # aplica los cambios
  python3 scripts/backfill_apro_ia_quote_status.py --dry-run    # solo reporta, no escribe en BD

Variables de entorno (desde .env raíz o shell):
  DATABASE_URL         — PostgreSQL (obligatoria)
  APRO_IA_BASE_URL      — QA: https://dms-qa.apro.cl / Prod: https://dms.apro.cl
  APRO_IA_BEARER_TOKEN  — token Bearer de APRO IA
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

_SERVICE = _ROOT / "src" / "services" / "quotes"
_SHARED = _ROOT / "src" / "shared"
for p in (_SERVICE, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from sqlalchemy import select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.quote_request import QuoteRequest  # noqa: E402
from services import apro_ia_client  # noqa: E402


def run(*, dry_run: bool) -> int:
    with get_session() as session:
        quotes = list(
            session.scalars(
                select(QuoteRequest)
                .where(QuoteRequest.apro_ia_folio.is_not(None))
                .order_by(QuoteRequest.created_at.asc())
            )
        )

        print(f"Cotizaciones con apro_ia_folio: {len(quotes)}")
        if not quotes:
            return 0

        updated = 0
        unchanged = 0
        errors: list[str] = []

        for quote in quotes:
            folio = quote.apro_ia_folio
            try:
                data = apro_ia_client.obtener_cotizacion(folio)
            except RuntimeError as e:
                errors.append(f"{folio} (quote {quote.id}): {e}")
                continue

            new_status = data.get("status")
            if not new_status:
                continue

            if quote.apro_ia_status == new_status:
                unchanged += 1
                continue

            print(f"  - {folio}: {quote.apro_ia_status!r} -> {new_status!r}")
            updated += 1
            if not dry_run:
                quote.apro_ia_status = new_status

        if not dry_run and updated:
            session.commit()

        print()
        print("=" * 60)
        print("Resumen" + (" [DRY-RUN]" if dry_run else ""))
        print("=" * 60)
        print(f"  Actualizadas: {updated}")
        print(f"  Sin cambios:  {unchanged}")
        print(f"  Errores:      {len(errors)}")

        if errors:
            print()
            print("Detalle de errores:")
            for err in errors:
                print(f"  - {err}")

        return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill: reconcilia apro_ia_status de cotizaciones ya procesadas.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Reporta los cambios sin escribirlos en la BD.",
    )
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
