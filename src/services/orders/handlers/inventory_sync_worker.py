"""Worker SQS — sync de stock por ``inventory_item_id``.

Consume la cola FIFO ``inventory-stock-sync.fifo`` (un mensaje por evento
``inventory_levels/update`` ya validado por HMAC). Por cada mensaje resuelve
shop+token, suma el stock entre locations activas y escribe el metafield
``aproclick.stock`` en la variante asociada.

Devuelve ``batchItemFailures`` para que SQS reentregue solo los mensajes
que fallaron (los exitosos no se vuelven a procesar).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext

service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

lambda_root = "/var/task"
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

possible_shared_paths = [
    Path("/var/task/shared"),
    service_root / "shared",
]
for path in possible_shared_paths:
    if path.exists() and (path / "database").exists():
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        break

from services.shopify_inventory_metafield import (  # noqa: E402
    SyncResult,
    resolve_shop_and_token,
    sync_variant_stock_from_inventory_item,
)

logger = Logger()
tracer = Tracer()


def _parse_record_body(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("body")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("mensaje SQS sin body")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"body SQS no es JSON válido: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("body SQS debe ser objeto JSON")
    return data


def _process(record: dict[str, Any]) -> SyncResult:
    payload = _parse_record_body(record)
    shop_domain = (payload.get("shop_domain") or "").strip()
    inventory_item_id = payload.get("inventory_item_id")
    if not shop_domain:
        raise ValueError("shop_domain requerido en el mensaje")
    if inventory_item_id is None:
        raise ValueError("inventory_item_id requerido en el mensaje")

    shop, token = resolve_shop_and_token(shop_domain)
    return sync_variant_stock_from_inventory_item(shop, token, inventory_item_id)


@tracer.capture_lambda_handler
def lambda_handler(event: dict, context: LambdaContext) -> dict:
    records = event.get("Records") or []
    failures: list[dict[str, str]] = []

    for record in records:
        message_id = record.get("messageId") if isinstance(record, dict) else None
        try:
            result = _process(record if isinstance(record, dict) else {})
        except LookupError as exc:
            # Tienda sin instalación: no hay nada que reintentar.
            logger.warning(
                "inventory sync skip — instalación no encontrada: %s", exc
            )
            continue
        except ValueError as exc:
            logger.exception("inventory sync — error de validación: %s", exc)
            if message_id:
                failures.append({"itemIdentifier": message_id})
            continue
        except Exception:
            logger.exception("inventory sync — error inesperado")
            if message_id:
                failures.append({"itemIdentifier": message_id})
            continue

        if result.skipped_reason:
            logger.info(
                "inventory sync skip",
                extra={
                    "inventory_item_id": result.inventory_item_id,
                    "reason": result.skipped_reason,
                },
            )
        else:
            logger.info(
                "inventory sync ok",
                extra={
                    "inventory_item_id": result.inventory_item_id,
                    "variant_gid": result.variant_gid,
                    "total_available": result.total_available,
                },
            )

    return {"batchItemFailures": failures}
