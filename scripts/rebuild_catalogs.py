#!/usr/bin/env python3
"""Reconstruye los Catalogs B2B en Shopify para uno o todos los segmentos.

Verifica que cada PriceList tenga un Catalog asociado con las CompanyLocations
correctas según la BD. Útil cuando se aprobaron empresas antes de que existiera
el catálogo, o para sincronizar después de cambios masivos de company_type.

Uso:
  python3 scripts/rebuild_catalogs.py                  # todos los segmentos
  python3 scripts/rebuild_catalogs.py --segment GRAN_EMPRESA
  python3 scripts/rebuild_catalogs.py --segment PYME --segment MEDIANA
  python3 scripts/rebuild_catalogs.py --dry-run        # solo muestra el estado actual

Variables de entorno (desde .env o shell):
  DATABASE_URL       — PostgreSQL (obligatoria)
  SHOPIFY_SHOP       — dominio .myshopify.com (opcional si hay una sola instalación)
  SHOPIFY_API_VERSION — default 2026-04
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent

# Cargar .env antes de cualquier import del servicio.
sys.path.insert(0, str(_SCRIPTS))
from dotenv_loader import load_root_dotenv_defaults  # noqa: E402

load_root_dotenv_defaults(_ROOT)

# Inyectar paths para importar el servicio prices y shared.
_SERVICE = _ROOT / "src" / "services" / "prices"
_SHARED = _ROOT / "src" / "shared"
for p in (_SERVICE, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from services.upload_orchestrator import rebuild_segment_catalog  # noqa: E402
from services.shopify_b2b import VALID_SEGMENTS  # noqa: E402

ALL_SEGMENTS: tuple[str, ...] = VALID_SEGMENTS


def _fmt(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=2)


def run_rebuild(segments: list[str], *, dry_run: bool) -> int:
    exit_code = 0
    for segment in segments:
        print(f"\n{'='*60}")
        print(f"Segmento: {segment}")
        print("=" * 60)

        if dry_run:
            # Solo consulta el estado en Shopify sin crear nada.
            try:
                from services.shop_resolver import resolve_shop_and_token
                from services.shopify_b2b import get_price_list_summary
                from services.upload_orchestrator import (
                    _get_segment_config,
                    _b2b_currency,
                )
                from database.engine import get_session
                from sqlalchemy import select
                from database.models.price_list import ShopifyPriceSegment

                shop, token = resolve_shop_and_token()

                with get_session() as session:
                    row = session.scalar(
                        select(ShopifyPriceSegment).where(
                            ShopifyPriceSegment.segment == segment
                        )
                    )
                    price_list_gid = row.price_list_gid if row else None
                    catalog_gid = row.catalog_gid if row else None
                    locations_csv = row.company_location_ids if row else None

                if not price_list_gid:
                    print(f"[DRY-RUN] Sin PriceList en BD — subir un Excel primero.")
                    continue

                try:
                    summary = get_price_list_summary(shop, token, price_list_gid)
                    print(f"[DRY-RUN] PriceList: {summary['name']} ({summary['id']})")
                    print(f"          Precios fijos: {summary.get('fixedPricesCount', '?')}")
                    cat = summary.get("catalog")
                    if cat:
                        print(f"          Catalog en Shopify: {cat.get('title')} ({cat.get('id')})")
                        print(f"          Estado: {cat.get('status')}")
                    else:
                        print("          Catalog en Shopify: NINGUNO — rebuild-catalog lo crearía")
                    print(f"          catalog_gid en BD: {catalog_gid or 'NULL'}")
                    print(f"          locations en BD:   {locations_csv or 'NULL'}")
                except LookupError as e:
                    print(f"[DRY-RUN] PriceList no encontrado en Shopify: {e}")
            except Exception as e:
                print(f"[DRY-RUN] Error consultando estado: {e}", file=sys.stderr)
                exit_code = 1
            continue

        # Rebuild real.
        try:
            result = rebuild_segment_catalog(segment)
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            exit_code = 1
            continue
        except Exception as e:
            print(f"[ERROR] Excepción inesperada: {e}", file=sys.stderr)
            exit_code = 1
            continue

        status = result.get("status", "")
        if status == "CREATED":
            print(f"[OK] Catalog creado")
            print(f"     catalog_gid:   {result.get('catalog_gid')}")
            print(f"     price_list:    {result.get('price_list_gid')}")
            print(f"     locations:     {len(result.get('company_location_ids') or [])}")
            print(f"     precios fijos: {result.get('fixed_prices_count', '?')}")
            print(f"     fuente:        {result.get('source')}")
        elif status == "SYNCED":
            print(f"[OK] Catalog sincronizado — locations actualizadas")
            print(f"     catalog:       {result.get('catalog_title')} ({result.get('catalog_gid')})")
            print(f"     estado:        {result.get('catalog_status')}")
            print(f"     locations:     {result.get('locations_synced', 0)}")
            print(f"     precios fijos: {result.get('fixed_prices_count', '?')}")
            print(f"     fuente:        {result.get('source')}")
        elif status == "ALREADY_LINKED":
            print(f"[OK] Catalog existe y sin companies para sincronizar")
            print(f"     catalog:       {result.get('catalog_title')} ({result.get('catalog_gid')})")
            print(f"     estado:        {result.get('catalog_status')}")
            print(f"     precios fijos: {result.get('fixed_prices_count', '?')}")
            if result.get('message'):
                print(f"     nota:          {result.get('message')}")
        elif status == "NO_LOCATIONS":
            print(f"[AVISO] Sin CompanyLocations para este segmento")
            print(f"        {result.get('message')}")
            print(f"        precios fijos en PriceList: {result.get('fixed_prices_count', '?')}")
        elif status == "FAILED":
            print(f"[ERROR] {result.get('error')}", file=sys.stderr)
            exit_code = 1
        else:
            print(_fmt(result))

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconstruye Catalogs B2B en Shopify para los segmentos indicados.",
    )
    parser.add_argument(
        "--segment",
        action="append",
        dest="segments",
        choices=list(ALL_SEGMENTS),
        metavar="SEGMENTO",
        help=f"Segmento a reconstruir: {', '.join(ALL_SEGMENTS)}. Repetible. Default: todos.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo muestra el estado actual en Shopify y BD, sin crear ni modificar nada.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Ruta al archivo .env (default: .env en el directorio actual).",
    )
    args = parser.parse_args()

    # Override dotenv si el usuario apuntó a otro archivo.
    if args.env_file != ".env":
        from dotenv_loader import parse_dotenv_file
        import os
        for k, v in parse_dotenv_file(Path(args.env_file).expanduser()).items():
            os.environ.setdefault(k, v)

    segments = args.segments or list(ALL_SEGMENTS)

    mode = "DRY-RUN" if args.dry_run else "REBUILD"
    print(f"rebuild_catalogs — modo: {mode}")
    print(f"Segmentos: {', '.join(segments)}")

    return run_rebuild(segments, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
