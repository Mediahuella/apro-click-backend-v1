#!/usr/bin/env python3
"""Backfill: medio de pago (`payment_gateway_names`) de los pedidos ya sincronizados.

La columna se agregó en la migración `027` y solo se puebla desde los webhooks
`orders/create` / `orders/updated`, así que los pedidos anteriores quedan en
`NULL` y el panel no puede distinguir cuáles fueron por transferencia. Este
script los completa consultando `paymentGatewayNames` a la Admin API de Shopify
en lotes.

Uso:
  python3 scripts/backfill_order_payment_gateways.py --dry-run   # reporta, no escribe
  python3 scripts/backfill_order_payment_gateways.py             # aplica
  python3 scripts/backfill_order_payment_gateways.py --all       # incluye los ya poblados

Variables de entorno (desde .env raíz o shell):
  DATABASE_URL          — PostgreSQL (obligatoria)
  SHOPIFY_API_VERSION   — opcional, default 2026-04
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent

sys.path.insert(0, str(_SCRIPTS))
from dotenv_loader import load_root_dotenv_defaults  # noqa: E402

load_root_dotenv_defaults(_ROOT)

_SERVICE = _ROOT / "src" / "services" / "orders"
_SHARED = _ROOT / "src" / "shared"
for p in (_SERVICE, _SHARED):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import os  # noqa: E402

from sqlalchemy import select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.shopify import ShopifyAppInstallation  # noqa: E402
from database.models.shopify_order import ShopifyOrder  # noqa: E402

#: Shopify acepta hasta 250 nodos por consulta; 50 deja margen de costo de query.
_LOTE = 50


def _api_version() -> str:
    return (os.environ.get("SHOPIFY_API_VERSION") or "2026-04").strip()


def _graphql(shop_domain: str, token: str, query: str, variables: dict) -> dict:
    req = urllib.request.Request(
        f"https://{shop_domain}/admin/api/{_api_version()}/graphql.json",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
    if body.get("errors"):
        raise RuntimeError(f"GraphQL: {body['errors']}")
    return body["data"]


_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Order { id name paymentGatewayNames }
  }
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no escribe en la BD")
    ap.add_argument(
        "--all",
        action="store_true",
        help="reprocesa también los pedidos que ya tienen medio de pago",
    )
    args = ap.parse_args()

    with get_session() as session:
        tokens = {
            inst.shop_domain: inst.shopify_access_token
            for inst in session.scalars(
                select(ShopifyAppInstallation).where(
                    ShopifyAppInstallation.uninstalled_at.is_(None)
                )
            ).all()
            if inst.shopify_access_token
        }
        if not tokens:
            print("No hay instalaciones Shopify activas con token.")
            return 1

        q = select(ShopifyOrder)
        if not args.all:
            q = q.where(ShopifyOrder.payment_gateway_names.is_(None))
        pedidos = list(session.scalars(q).all())
        print(f"Pedidos a procesar: {len(pedidos)}")
        if not pedidos:
            return 0

        por_tienda: dict[str, list[ShopifyOrder]] = {}
        for row in pedidos:
            por_tienda.setdefault(row.shop_domain, []).append(row)

        actualizados = 0
        sin_datos = 0
        for shop_domain, filas in por_tienda.items():
            token = tokens.get(shop_domain)
            if not token:
                print(f"  {shop_domain}: sin token activo, se omiten {len(filas)} pedidos")
                continue
            for i in range(0, len(filas), _LOTE):
                lote = filas[i : i + _LOTE]
                ids = [f"gid://shopify/Order/{r.shopify_order_id}" for r in lote]
                data = _graphql(shop_domain, token, _QUERY, {"ids": ids})
                por_gid = {
                    n["id"]: n.get("paymentGatewayNames") or []
                    for n in data.get("nodes") or []
                    if n
                }
                for row in lote:
                    gid = f"gid://shopify/Order/{row.shopify_order_id}"
                    gateways = por_gid.get(gid)
                    if gateways is None:
                        sin_datos += 1
                        print(f"  {row.order_name}: no encontrado en Shopify")
                        continue
                    gateways = [str(g).strip() for g in gateways if str(g or "").strip()]
                    print(f"  {row.order_name}: {gateways or '(sin medio de pago)'}")
                    if not args.dry_run:
                        row.payment_gateway_names = gateways or None
                    actualizados += 1

        if args.dry_run:
            print(f"\n[dry-run] {actualizados} pedidos se actualizarían, {sin_datos} sin datos.")
        else:
            session.commit()
            print(f"\n{actualizados} pedidos actualizados, {sin_datos} sin datos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
