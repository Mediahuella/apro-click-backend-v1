#!/usr/bin/env python3
"""Sync masivo del metafield ``aproclick.stock`` vía Shopify Admin GraphQL.

Bulk ``bulkOperationRunQuery`` sobre ``products`` → ``variants``, descarga
JSONL y aplica ``metafieldsSet`` en lotes (mismo flujo que el script Node
previo).

Credenciales: ``SHOPIFY_SHOP``, ``SHOPIFY_ADMIN_TOKEN``; opcionales:
``SHOPIFY_API_VERSION``, ``SHOPIFY_STOCK_NAMESPACE`` / ``SHOPIFY_STOCK_KEY``
(o ``STOCK_NAMESPACE`` / ``STOCK_KEY``).

Fusiona ``.env`` desde la cwd (no pisa variables ya definidas). Vé también
``npm run sync:stock``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

HTTP_TIMEOUT_QUERY = 120
HTTP_TIMEOUT_DOWNLOAD = 300

# GraphQL mutation con block string triple-comilla dentro; capa exterior en ''' '''
BULK_MUTATION = '''mutation BulkStockExport {
  bulkOperationRunQuery(
    query: """
{
  products {
    edges {
      node {
        id
        variants {
          edges {
            node {
              id
              inventoryQuantity
              inventoryItem {
                id
                tracked
              }
            }
          }
        }
      }
    }
  }
}
"""
  ) {
    bulkOperation {
      id
      status
    }
    userErrors {
      field
      message
    }
  }
}
'''

CURRENT_BULK = """
query CB {
  currentBulkOperation {
    id
    status
    errorCode
    url
    partialDataUrl
  }
}
"""

METAFIELDS_SET = """
mutation SetStock($mf: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $mf) {
    userErrors {
      field
      message
      code
    }
    metafields {
      id
    }
  }
}
"""




def load_env_file(rel: str) -> None:
    """Carga claves desde ``rel`` solo si ``os.environ`` no tiene la clave."""
    p = Path(rel).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    if not p.is_file():
        return
    for raw_line in p.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if key and os.environ.get(key) is None:
            os.environ[key] = val


def shopify_graphql(
    shop_domain: str,
    token: str,
    api_version: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"
    payload: dict[str, Any] = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_QUERY) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GraphQL HTTP {e.code}: {raw[:1200]}") from e

    errors = parsed.get("errors") or []
    if errors:
        msg = "; ".join(str(e.get("message", e)) for e in errors)
        raise RuntimeError(f"GraphQL errors: {msg}")
    data = parsed.get("data")
    if data is None:
        raise RuntimeError(f"Respuesta GraphQL sin data: {parsed!s}"[:1200])
    return data


def wait_bulk_complete(shop_domain: str, token: str, api_version: str, poll_ms: int = 3000) -> dict[str, Any]:
    while True:
        data = shopify_graphql(shop_domain, token, api_version, CURRENT_BULK)
        op = data.get("currentBulkOperation") or {}
        st = (op.get("status") or "").upper()
        if st == "COMPLETED":
            return op
        if st in {"FAILED", "CANCELED"}:
            ec = op.get("errorCode") or "unknown"
            partial = op.get("partialDataUrl") or ""
            raise RuntimeError(f"Bulk operation {st}: {ec} {partial}")
        time.sleep(poll_ms / 1000.0)


def download_text(url: str) -> str:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_DOWNLOAD) as resp:
        return resp.read().decode("utf-8")


def parse_variants_from_jsonl(text: str, only_positive: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        oid = obj.get("id")
        if not isinstance(oid, str) or "ProductVariant" not in oid:
            continue
        tracked = None
        inv_item = obj.get("inventoryItem")
        if isinstance(inv_item, dict):
            tracked = inv_item.get("tracked")
        if tracked is False:
            continue

        qty = obj.get("inventoryQuantity")
        if qty is None:
            continue
        try:
            qn = int(qty)
        except (TypeError, ValueError):
            continue
        if only_positive and qn <= 0:
            continue
        out.append({"variantGid": oid, "value": int(qn)})
    return out


def is_throttled_user_errors(user_errors: list[dict[str, Any]]) -> bool:
    for e in user_errors:
        blob = f"{e.get('code','')} {e.get('message','')}".upper()
        if "THROTTLE" in blob:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk sync metafield aproclick.stock")
    parser.add_argument("--dry-run", action="store_true", help="Solo cuenta, no escribe")
    parser.add_argument(
        "--only-positive",
        action="store_true",
        help="Solo variantes con inventoryQuantity > 0",
    )
    parser.add_argument("--reuse", action="store_true", help="Reutiliza JSONL de --out")
    parser.add_argument("--env-file", default=".env", help="Archivo dotenv (default .env)")
    parser.add_argument("--out", default="./bulk-stock.jsonl", dest="out_path")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--rate-ms", type=int, default=250)
    args = parser.parse_args()

    load_env_file(args.env_file)

    shop = (os.environ.get("SHOPIFY_SHOP") or "").strip()
    token = (os.environ.get("SHOPIFY_ADMIN_TOKEN") or "").strip()
    api_ver = (os.environ.get("SHOPIFY_API_VERSION") or "2026-04").strip()
    ns = (
        os.environ.get("STOCK_NAMESPACE")
        or os.environ.get("SHOPIFY_STOCK_NAMESPACE")
        or "aproclick"
    ).strip()
    key = (
        os.environ.get("STOCK_KEY") or os.environ.get("SHOPIFY_STOCK_KEY") or "stock"
    ).strip()

    if not shop or not token:
        print(
            "Faltan SHOPIFY_SHOP y/o SHOPIFY_ADMIN_TOKEN (.env o entorno).",
            file=sys.stderr,
        )
        return 1

    shop_norm = shop.removeprefix("https://").strip().rstrip("/")

    outfile = Path(args.out_path).expanduser()
    if not outfile.is_absolute():
        outfile = Path.cwd() / outfile

    if args.reuse and outfile.is_file():
        print("--reuse: leyendo", outfile)
        jsonl_text = outfile.read_text(encoding="utf-8")
    else:
        run = shopify_graphql(shop_norm, token, api_ver, BULK_MUTATION)
        ue = (run.get("bulkOperationRunQuery") or {}).get("userErrors") or []
        if ue:
            print("bulkOperationRunQuery userErrors:", json.dumps(ue, indent=2), file=sys.stderr)
            return 1

        print("Esperando bulk operation (poll cada 3s)…")
        op = wait_bulk_complete(shop_norm, token, api_ver)

        dl_url = op.get("url") or op.get("partialDataUrl")
        if not dl_url:
            print("Bulk completado sin URL de descarga.", file=sys.stderr)
            return 1

        print("Descargando resultado…")
        jsonl_text = download_text(dl_url)
        outfile.parent.mkdir(parents=True, exist_ok=True)
        outfile.write_text(jsonl_text, encoding="utf-8")
        print("JSONL guardado en", outfile)

    variants = parse_variants_from_jsonl(jsonl_text, args.only_positive)
    label = "Variantes con stock > 0" if args.only_positive else "Variantes relevantes"
    print(f"{label}: {len(variants)}")

    if args.dry_run or not variants:
        if args.dry_run:
            print("Dry-run: no se escribe metafieldsSet.")
        return 0

    max_batch = min(max(1, args.batch_size), 25)
    written = 0
    errors = 0

    pending = list(variants)

    while pending:
        batch = pending[:max_batch]
        del pending[:max_batch]
        mf = [
            {
                "ownerId": v["variantGid"],
                "namespace": ns,
                "key": key,
                "type": "number_integer",
                "value": str(v["value"]),
            }
            for v in batch
        ]

        for attempt in range(9):
            try:
                data = shopify_graphql(shop_norm, token, api_ver, METAFIELDS_SET, {"mf": mf})
                ue = (data.get("metafieldsSet") or {}).get("userErrors") or []
                if is_throttled_user_errors(ue) and attempt < 8:
                    pause = max(args.rate_ms * (attempt + 1) * 2, 1500) / 1000.0
                    time.sleep(pause)
                    continue
                if ue:
                    print("metafieldsSet userErrors:", json.dumps(ue, indent=2), file=sys.stderr)
                    errors += len(ue)
                else:
                    written += len(mf)
                break
            except (RuntimeError, OSError, urllib.error.URLError) as e:
                if attempt >= 8:
                    errors += len(mf)
                    print(e, file=sys.stderr)
                    break
                pause = max(args.rate_ms * (attempt + 1) * 2, 1500) / 1000.0
                time.sleep(pause)

        time.sleep(max(0, args.rate_ms / 1000.0))

    print(f"Listo: metafields escritos (aprox): {written}, errores reportados: {errors}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
