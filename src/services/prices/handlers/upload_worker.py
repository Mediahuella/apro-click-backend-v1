"""Worker SQS — procesa los Excel subidos al servicio prices.

Para cada mensaje (``{"upload_id": "<uuid>"}``) ejecuta el pipeline
``services.upload_orchestrator.process_upload`` que:

1. Descarga el Excel desde S3.
2. Lo parsea.
3. Resuelve SAP → ProductVariant GID en Shopify.
4. Sube los precios a 3 PriceLists / Catalogs B2B (PYME / MEDIANA /
   GRAN_EMPRESA) usando ``bulkOperationRunMutation`` (en serie, por
   restricción de Shopify).

Errores fatales (parser inválido, sin variantes, S3 caído) marcan la fila
``price_list_uploads`` como ``FAILED`` y no se reintentan. Errores transitorios
(timeout polling, etc.) dejan la fila en ``PUSHED`` y se pueden refrescar
desde el endpoint ``/api/v1/prices/uploads/{id}/refresh``.

Devolvemos ``batchItemFailures`` para que SQS reentregue sólo los mensajes
que sí queremos reintentar (red caída, lock optimista de la BD, etc.).
"""
from __future__ import annotations

import json
import sys
import uuid as uuid_mod
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

from services.upload_orchestrator import process_upload  # noqa: E402

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


def _process(record: dict[str, Any]) -> dict[str, Any]:
    payload = _parse_record_body(record)
    raw_upload = payload.get("upload_id") or payload.get("uploadId")
    if not raw_upload:
        raise ValueError("upload_id requerido en el mensaje SQS")
    try:
        uid = uuid_mod.UUID(str(raw_upload))
    except ValueError as e:
        raise ValueError(f"upload_id inválido: {raw_upload!r}") from e
    return process_upload(uid)


@tracer.capture_lambda_handler
def lambda_handler(event: dict, context: LambdaContext) -> dict:
    records = event.get("Records") or []
    failures: list[dict[str, str]] = []

    for record in records:
        message_id = record.get("messageId") if isinstance(record, dict) else None
        try:
            result = _process(record if isinstance(record, dict) else {})
            logger.info(
                "price upload procesado",
                extra={"message_id": message_id, **result},
            )
        except LookupError as exc:
            # Upload no encontrado → no reintentar.
            logger.warning("price upload skip: %s", exc)
            continue
        except ValueError as exc:
            # Mensaje inválido / Excel corrupto → no reintentar.
            logger.exception("price upload validation error: %s", exc)
            continue
        except Exception:
            logger.exception("price upload error inesperado")
            if message_id:
                failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}
