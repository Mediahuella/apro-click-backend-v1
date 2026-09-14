#!/usr/bin/env python3
"""Backfill: metafield `aproclick.credito` en las Companies B2B de Shopify.

La Shopify Function `manejo-de-credito` decide si muestra el pago con crédito
en el checkout leyendo ese metafield de la Company. El CRM es la fuente de
verdad (`companies.payment_type`), pero el metafield solo se escribe desde ahora
al aprobar una solicitud o al editar la empresa en el panel: las empresas
anteriores no lo tienen y la Function las trata como **sin** crédito.

Este script las recorre y escribe el valor que corresponde según el CRM.

Uso:
  python3 scripts/backfill_company_credit_metafield.py --dry-run   # reporta, no escribe
  python3 scripts/backfill_company_credit_metafield.py             # aplica
  python3 scripts/backfill_company_credit_metafield.py --only-credit  # solo las CREDIT

Variables de entorno (desde .env raíz o shell):
  DATABASE_URL          — PostgreSQL (obligatoria)
  SHOPIFY_SHOP          — opcional, fuerza la tienda (si hay más de una instalada)
  SHOPIFY_API_VERSION   — opcional, default 2026-04
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
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

import os  # noqa: E402

from sqlalchemy import select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.company import Company  # noqa: E402
from database.models.shopify import ShopifyAppInstallation  # noqa: E402

NAMESPACE = "aproclick"
KEY = "credito"

#: `metafieldsSet` acepta hasta 25 metafields por llamada.
_LOTE = 25

_MUTATION = """
mutation SetCompanyCredito($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id namespace key value owner { ... on Company { id name } } }
    userErrors { field message code }
  }
}
"""


def _api_version() -> str:
    return (os.environ.get("SHOPIFY_API_VERSION") or "2026-04").strip()


def _graphql(shop_domain: str, token: str, query: str, variables: dict) -> dict:
    req = urllib.request.Request(
        f"https://{shop_domain}/admin/api/{_api_version()}/graphql.json",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
    if body.get("errors"):
        raise RuntimeError(f"GraphQL: {body['errors']}")
    return body["data"]


def _company_gid(value: str) -> str:
    s = str(value or "").strip()
    if not s:
        return ""
    return s if s.startswith("gid://shopify/Company/") else f"gid://shopify/Company/{s}"


def _resolve_store() -> tuple[str, str]:
    shop_env = (os.environ.get("SHOPIFY_SHOP") or "").strip()
    with get_session() as session:
        q = select(ShopifyAppInstallation).where(
            ShopifyAppInstallation.uninstalled_at.is_(None),
            ShopifyAppInstallation.shopify_access_token.is_not(None),
        )
        if shop_env:
            domain = shop_env if shop_env.endswith(".myshopify.com") else f"{shop_env}.myshopify.com"
            q = q.where(ShopifyAppInstallation.shop_domain == domain.lower())
        filas = list(session.scalars(q).all())
    if not filas:
        raise RuntimeError("No hay instalación Shopify activa con token")
    if len(filas) > 1:
        dominios = ", ".join(f.shop_domain for f in filas)
        raise RuntimeError(f"Más de una tienda activa ({dominios}): definir SHOPIFY_SHOP")
    return filas[0].shop_domain, filas[0].shopify_access_token.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no escribe en Shopify")
    ap.add_argument(
        "--only-credit",
        action="store_true",
        help="solo las empresas con payment_type=CREDIT (no repone los false)",
    )
    args = ap.parse_args()

    shop_domain, token = _resolve_store()
    print(f"Tienda: {shop_domain}")

    with get_session() as session:
        q = select(Company).where(
            Company.shopify_company_id.is_not(None),
            Company.is_system.is_(False),
        )
        if args.only_credit:
            q = q.where(Company.payment_type == "CREDIT")
        empresas = [
            (c.name, _company_gid(c.shopify_company_id), c.payment_type)
            for c in session.scalars(q.order_by(Company.name)).all()
        ]

    empresas = [e for e in empresas if e[1]]
    con_credito = sum(1 for _, _, pt in empresas if pt == "CREDIT")
    print(f"Empresas a sincronizar: {len(empresas)} ({con_credito} con crédito)")
    if not empresas:
        return 0

    if args.dry_run:
        for nombre, gid, pt in empresas:
            print(f"  [dry-run] {nombre}: {NAMESPACE}.{KEY} = {'true' if pt == 'CREDIT' else 'false'}")
        return 0

    escritos = 0
    for i in range(0, len(empresas), _LOTE):
        lote = empresas[i : i + _LOTE]
        metafields = [
            {
                "ownerId": gid,
                "namespace": NAMESPACE,
                "key": KEY,
                "type": "boolean",
                "value": "true" if pt == "CREDIT" else "false",
            }
            for _, gid, pt in lote
        ]
        data = _graphql(shop_domain, token, _MUTATION, {"metafields": metafields})
        payload = data.get("metafieldsSet") or {}
        errs = payload.get("userErrors") or []
        if errs:
            print(f"  userErrors en el lote {i // _LOTE + 1}: {errs}")
        escritos += len(payload.get("metafields") or [])
        for nombre, _, pt in lote:
            print(f"  {nombre}: {'true' if pt == 'CREDIT' else 'false'}")

    print(f"\nMetafields escritos: {escritos}/{len(empresas)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
