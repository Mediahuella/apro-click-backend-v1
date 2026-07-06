#!/usr/bin/env python3
"""Backfill: carga masiva de empresas existentes hacia APRO IA (DMS externo).

Lee todas las `Company` de la BD (con su `Client` de contacto asociado), arma
un Excel en memoria con las columnas que espera APRO IA (RUT, Nombre, SN,
Correo, Teléfono, Fuente) y lo sube vía `POST /clients/upsert-excel`.

Uso:
  python3 scripts/backfill_apro_ia_clients.py              # sube el Excel real
  python3 scripts/backfill_apro_ia_clients.py --dry-run    # solo arma el Excel y lo reporta, no sube

Variables de entorno (desde .env raíz o shell):
  DATABASE_URL             — PostgreSQL (obligatoria)
  APRO_IA_CLIENTS_BASE_URL — QA: https://cliente-qa.apro.cl / Prod: https://clientes.apro.cl
  APRO_IA_BEARER_TOKEN     — token Bearer de APRO IA
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

from openpyxl import Workbook  # noqa: E402
from sqlalchemy import select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.client import Client  # noqa: E402
from database.models.company import Company  # noqa: E402
from utils.apro_ia_client import upsert_excel  # noqa: E402
from utils.rut import normalize_rut_digits  # noqa: E402

_COLUMNS = ["RUT", "Nombre SN", "Correo", "Teléfono", "Fuente"]


def _load_rows() -> list[dict[str, str]]:
    """Empresas con `billing_rut` y su primer `Client` de contacto (si existe)."""
    with get_session() as session:
        companies = list(
            session.scalars(
                select(Company)
                .where(Company.billing_rut.is_not(None))
                .where(Company.is_system.is_(False))
                .order_by(Company.created_at.asc())
            )
        )

        rows: list[dict[str, str]] = []
        omitidas: list[str] = []
        for company in companies:
            try:
                rut = normalize_rut_digits(company.billing_rut)
            except ValueError:
                omitidas.append(f"{company.name} — billing_rut inválido ({company.billing_rut!r})")
                continue

            client = session.scalar(
                select(Client)
                .where(Client.company_id == company.id)
                .order_by(Client.created_at.asc())
                .limit(1)
            )
            rows.append(
                {
                    "RUT": rut,
                    "Nombre SN": company.name,
                    "Correo": (client.email if client else "") or "",
                    "Teléfono": (client.phone if client else "") or "",
                    "Fuente": "AproClick",
                }
            )

    if omitidas:
        print("Empresas omitidas (RUT inválido):", file=sys.stderr)
        for o in omitidas:
            print(f"  - {o}", file=sys.stderr)

    return rows


def _build_excel_bytes(rows: list[dict[str, str]]) -> bytes:
    from io import BytesIO

    wb = Workbook()
    ws = wb.active
    ws.title = "Clientes"
    ws.append(_COLUMNS)
    for row in rows:
        ws.append([row[col] for col in _COLUMNS])

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def run(*, dry_run: bool) -> int:
    rows = _load_rows()
    print(f"Empresas a sincronizar: {len(rows)}")
    if not rows:
        return 0

    excel_bytes = _build_excel_bytes(rows)

    if dry_run:
        print("[DRY-RUN] Excel armado en memoria, no se sube a APRO IA.")
        for row in rows[:10]:
            print(f"  - {row['RUT']}  {row['Nombre SN']}  {row['Correo']}  {row['Teléfono']}")
        if len(rows) > 10:
            print(f"  ... y {len(rows) - 10} más")
        return 0

    print("Subiendo Excel a APRO IA (POST /clients/upsert-excel)...")
    try:
        result = upsert_excel(excel_bytes, filename="backfill-empresas-aproclick.xlsx")
    except RuntimeError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    created = result.get("created", result.get("data", {}).get("created"))
    updated = result.get("updated", result.get("data", {}).get("updated"))
    errors = result.get("errors", result.get("data", {}).get("errors")) or []

    print()
    print("=" * 60)
    print("Resumen")
    print("=" * 60)
    print(f"  Creados:   {created}")
    print(f"  Actualizados: {updated}")
    print(f"  Errores:   {len(errors)}")

    if errors:
        print()
        print("Detalle de errores:")
        for err in errors:
            print(f"  - {err}")

    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill: sube todas las empresas existentes a APRO IA vía upsert-excel.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Arma el Excel y lo reporta, sin subirlo a APRO IA.",
    )
    args = parser.parse_args()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
