"""Worker SQS — procesa los Excel de precios B2C/WEB.

Para cada mensaje (``{"upload_id": "<uuid>"}``) ejecuta
``services.b2c_orchestrator.process_b2c_upload``, que actualiza el precio por
defecto (``price`` + ``compareAtPrice``) de las variantes de la tienda B2C.

Para **no saturar el Lambda**, calcula un *deadline* a partir del tiempo
restante de la invocación (menos un margen) y se lo pasa al orquestador: si el
diff no alcanza a procesarse completo, el orquestador re-encola el mismo upload
y la siguiente invocación continúa (es idempotente).

Errores fatales (Excel sin precios, sin coincidencias de SKU, S3 caído) marcan
la fila como ``FAILED`` y no se reintentan; errores inesperados sí se reintentan
vía ``batchItemFailures``.
"""
from __future__ import annotations

import json
import sys
import time
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

from services.b2c_orchestrator import process_b2c_upload  # noqa: E402

logger = Logger()
tracer = Tracer()

#: Margen reservado al final de la invocación para escribir en BD y re-encolar
#: la continuación antes de que el Lambda muera.
_DEADLINE_MARGIN_SEC = 60.0
#: Fallback si no hay contexto Lambda (tests locales): ~13 min.
_DEFAULT_BUDGET_SEC = 780.0


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


def _deadline_from_context(context: LambdaContext | None) -> float:
    remaining_ms = None
    if context is not None:
        try:
            remaining_ms = context.get_remaining_time_in_millis()
        except Exception:
            remaining_ms = None
    budget = (
        remaining_ms / 1000.0 if remaining_ms else _DEFAULT_BUDGET_SEC
    ) - _DEADLINE_MARGIN_SEC
    return time.monotonic() + max(budget, 30.0)


def _process(record: dict[str, Any], context: LambdaContext | None) -> dict[str, Any]:
    payload = _parse_record_body(record)
    raw_upload = payload.get("upload_id") or payload.get("uploadId")
    if not raw_upload:
        raise ValueError("upload_id requerido en el mensaje SQS")
    try:
        uid = uuid_mod.UUID(str(raw_upload))
    except ValueError as e:
        raise ValueError(f"upload_id inválido: {raw_upload!r}") from e
    return process_b2c_upload(uid, deadline_monotonic=_deadline_from_context(context))


@tracer.capture_lambda_handler
def lambda_handler(event: dict, context: LambdaContext) -> dict:
    records = event.get("Records") or []
    failures: list[dict[str, str]] = []

    for record in records:
        message_id = record.get("messageId") if isinstance(record, dict) else None
        try:
            result = _process(record if isinstance(record, dict) else {}, context)
            logger.info(
                "b2c price upload procesado",
                extra={"message_id": message_id, **result},
            )
        except LookupError as exc:
            logger.warning("b2c price upload skip: %s", exc)
            continue
        except ValueError as exc:
            logger.exception("b2c price upload validation error: %s", exc)
            continue
        except Exception:
            logger.exception("b2c price upload error inesperado")
            if message_id:
                failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}
