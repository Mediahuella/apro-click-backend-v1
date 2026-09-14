"""Worker SQS — envío de notificaciones por email vía AWS SES.

Consume la cola NotificationsQueue. Cada mensaje indica el tipo de
notificación y sus parámetros. Devuelve batchItemFailures para que SQS
solo reintente los mensajes que fallaron.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger
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

from notifications.ses_email import (  # noqa: E402
    notify_approval,
    notify_confirmacion_solicitud,
    notify_cotizacion_apro_ia_estado,
    notify_cotizacion_lista,
    notify_new_request,
    notify_nueva_cotizacion,
    notify_rejection,
    notify_transferencia_estado,
)

logger = Logger()

_HANDLERS: dict[str, Any] = {
    "nueva_solicitud": lambda p: notify_new_request(
        admin_emails=p["admin_emails"],
        company_name=p["company_name"],
        rut=p["rut"],
        contact_name=p["contact_name"],
        contact_email=p["contact_email"],
        request_id=p["request_id"],
    ),
    "solicitud_aprobada": lambda p: notify_approval(
        contact_email=p["contact_email"],
        contact_name=p["contact_name"],
        company_name=p["company_name"],
    ),
    "solicitud_rechazada": lambda p: notify_rejection(
        contact_email=p["contact_email"],
        contact_name=p["contact_name"],
        company_name=p["company_name"],
        reason=p.get("reason"),
    ),
    "confirmacion_solicitud": lambda p: notify_confirmacion_solicitud(
        contact_email=p["contact_email"],
        contact_name=p.get("contact_name"),
        company_name=p["company_name"],
        quote_id=p["quote_id"],
        lines=p.get("lines") or [],
    ),
    "nueva_cotizacion": lambda p: notify_nueva_cotizacion(
        admin_emails=p["admin_emails"],
        company_name=p["company_name"],
        quote_id=p["quote_id"],
        line_count=p["line_count"],
        contact_name=p.get("contact_name"),
        contact_email=p.get("contact_email"),
    ),
    "cotizacion_lista": lambda p: notify_cotizacion_lista(
        contact_email=p["contact_email"],
        contact_name=p.get("contact_name"),
        company_name=p["company_name"],
        folio=p["folio"],
        pdf_url=p["pdf_url"],
        total=p.get("total"),
    ),
    "cotizacion_apro_ia_estado": lambda p: notify_cotizacion_apro_ia_estado(
        admin_emails=p["admin_emails"],
        company_name=p["company_name"],
        folio=p["folio"],
        quote_id=p["quote_id"],
        event=p["event"],
        status=p["status"],
    ),
    "transferencia_estado": lambda p: notify_transferencia_estado(
        admin_emails=p["admin_emails"],
        company_name=p["company_name"],
        folio=p["folio"],
        quote_id=p["quote_id"],
        transfer_folio=p.get("transfer_folio", ""),
        estado=p["estado"],
        status=p["status"],
        # Mensajes encolados antes del webhook de créditos no traen el campo:
        # por definición son transferencias.
        payment_method=p.get("payment_method", "transferencia"),
    ),
}


def _procesar(record: dict[str, Any]) -> None:
    raw = record.get("body", "")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("mensaje SQS sin body")
    data = json.loads(raw)
    tipo = data.get("tipo")
    params = data.get("params") or {}
    handler = _HANDLERS.get(tipo)
    if not handler:
        logger.warning("tipo de notificación desconocido — ignorando", extra={"tipo": tipo})
        return
    handler(params)


def lambda_handler(event: dict, context: LambdaContext) -> dict:
    records = event.get("Records") or []
    failures: list[dict[str, str]] = []

    for record in records:
        message_id = record.get("messageId") if isinstance(record, dict) else None
        try:
            _procesar(record if isinstance(record, dict) else {})
        except Exception:
            logger.exception(
                "email_worker — error procesando mensaje",
                extra={"messageId": message_id},
            )
            if message_id:
                failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}
