"""Reconciliación periódica del metafield ``aproclick.stock`` vía Bulk Operation.

Recorre todas las variantes de la tienda con un Bulk Export
(``products`` → ``variants``) y escribe ``aproclick.stock = inventoryQuantity``
en lotes con ``metafieldsSet``. Pensado para correr en un schedule horario
(EventBridge) como safety-net ante eventos ``inventory_levels/update`` perdidos
o no entregados por webhook.

A diferencia del worker SQS (``shopify_inventory_metafield``), que suma
``available`` solo entre locations activas, aquí se usa el ``inventoryQuantity``
total que ya expone la variante. Diferencia menor aceptada a cambio de un job
simple y eficiente en llamadas a la Admin API (un único Bulk Export en lugar de
una query por item).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from urllib.request import Request, urlopen

from services.shopify_inventory_metafield import stock_key, stock_namespace
from utils.shopify_graphql import format_user_errors, graphql_call

# El bloque interno usa triple-comilla doble; por eso la capa externa va en ''' '''.
_BULK_EXPORT = '''mutation BulkStockExport {
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
              inventoryItem { id tracked }
            }
          }
        }
      }
    }
  }
}
"""
  ) {
    bulkOperation { id status }
    userErrors { field message }
  }
}
'''

_CURRENT_BULK = """
query CurrentBulk {
  currentBulkOperation {
    id
    status
    errorCode
    url
    partialDataUrl
  }
}
"""

_METAFIELDS_SET = """
mutation StockMetafieldSet($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    userErrors { field message code }
  }
}
"""

_BATCH_SIZE = 25
_DOWNLOAD_TIMEOUT = 120


@dataclass(frozen=True)
class ReconcileResult:
    """Resultado de reconciliar el stock de una tienda."""

    shop_domain: str
    variants: int
    written: int
    errors: int
    skipped_reason: str | None = None


def _is_throttled(message: str) -> bool:
    return "throttl" in (message or "").lower()


def _user_errors_throttled(user_errors: list[dict]) -> bool:
    for e in user_errors:
        blob = f"{e.get('code', '')} {e.get('message', '')}".upper()
        if "THROTTLE" in blob:
            return True
    return False


def _start_bulk_export(shop_domain: str, access_token: str) -> str | None:
    """Dispara el Bulk Export. Devuelve un motivo de skip o ``None`` si arrancó."""
    data = graphql_call(shop_domain, access_token, _BULK_EXPORT)
    payload = data.get("bulkOperationRunQuery") or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        msg = format_user_errors(user_errors)
        # Shopify rechaza un nuevo bulk si ya hay uno en curso para la tienda.
        if "already in progress" in msg.lower():
            return "bulk_already_running"
        raise ValueError(f"bulkOperationRunQuery falló: {msg}")
    return None


def _wait_bulk_complete(
    shop_domain: str,
    access_token: str,
    deadline: float,
    poll_seconds: float = 3.0,
) -> dict:
    while True:
        data = graphql_call(shop_domain, access_token, _CURRENT_BULK)
        op = data.get("currentBulkOperation") or {}
        status = (op.get("status") or "").upper()
        if status == "COMPLETED":
            return op
        if status in {"FAILED", "CANCELED", "EXPIRED"}:
            raise ValueError(
                f"Bulk operation {status}: {op.get('errorCode') or 'desconocido'}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Bulk operation no completó dentro del presupuesto (status={status})"
            )
        time.sleep(poll_seconds)


def _download_jsonl(url: str) -> str:
    req = Request(url, method="GET")
    with urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
        return resp.read().decode("utf-8")


def _parse_variants(text: str, only_positive: bool) -> list[dict]:
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        oid = obj.get("id")
        if not isinstance(oid, str) or "ProductVariant" not in oid:
            continue
        inv_item = obj.get("inventoryItem")
        if isinstance(inv_item, dict) and inv_item.get("tracked") is False:
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
        out.append({"variantGid": oid, "value": qn})
    return out


def _write_metafields(
    shop_domain: str,
    access_token: str,
    variants: list[dict],
    deadline: float,
    rate_seconds: float = 0.25,
) -> tuple[int, int]:
    namespace = stock_namespace()
    key = stock_key()
    written = 0
    errors = 0
    pending = list(variants)

    while pending:
        if time.monotonic() >= deadline:
            raise TimeoutError("Presupuesto agotado escribiendo metafields")
        batch = pending[:_BATCH_SIZE]
        del pending[:_BATCH_SIZE]
        metafields = [
            {
                "ownerId": v["variantGid"],
                "namespace": namespace,
                "key": key,
                "type": "number_integer",
                "value": str(v["value"]),
            }
            for v in batch
        ]

        for attempt in range(6):
            try:
                data = graphql_call(
                    shop_domain,
                    access_token,
                    _METAFIELDS_SET,
                    {"metafields": metafields},
                )
            except ValueError as exc:
                if _is_throttled(str(exc)) and attempt < 5:
                    time.sleep(max(rate_seconds * (attempt + 1) * 2, 1.5))
                    continue
                errors += len(metafields)
                break

            user_errors = (data.get("metafieldsSet") or {}).get("userErrors") or []
            if _user_errors_throttled(user_errors) and attempt < 5:
                time.sleep(max(rate_seconds * (attempt + 1) * 2, 1.5))
                continue
            if user_errors:
                errors += len(user_errors)
            else:
                written += len(metafields)
            break

        time.sleep(rate_seconds)

    return written, errors


def reconcile_shop_stock(
    shop_domain: str,
    access_token: str,
    *,
    only_positive: bool = False,
    time_budget_seconds: float = 840.0,
) -> ReconcileResult:
    """Reconcilia ``aproclick.stock`` para todas las variantes de una tienda.

    Lanza un Bulk Export, espera a que complete, descarga el JSONL y escribe el
    metafield en lotes. ``time_budget_seconds`` acota el tiempo total para no
    chocar con el timeout del Lambda.
    """
    deadline = time.monotonic() + time_budget_seconds

    skip = _start_bulk_export(shop_domain, access_token)
    if skip:
        return ReconcileResult(
            shop_domain=shop_domain,
            variants=0,
            written=0,
            errors=0,
            skipped_reason=skip,
        )

    op = _wait_bulk_complete(shop_domain, access_token, deadline)
    url = op.get("url") or op.get("partialDataUrl")
    if not url:
        # Tienda sin productos: bulk completa sin URL de descarga.
        return ReconcileResult(
            shop_domain=shop_domain,
            variants=0,
            written=0,
            errors=0,
            skipped_reason="no_data",
        )

    variants = _parse_variants(_download_jsonl(url), only_positive)
    if not variants:
        return ReconcileResult(
            shop_domain=shop_domain,
            variants=0,
            written=0,
            errors=0,
            skipped_reason="no_variants",
        )

    written, errors = _write_metafields(shop_domain, access_token, variants, deadline)
    return ReconcileResult(
        shop_domain=shop_domain,
        variants=len(variants),
        written=written,
        errors=errors,
    )
