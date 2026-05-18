"""Publica notificaciones en la cola SQS del servicio notifications."""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _sqs():
    return boto3.client("sqs")


def _encolar(tipo: str, params: dict) -> None:
    """Encola un mensaje de notificación. Non-blocking: loguea si falla."""
    queue_url = os.environ.get("NOTIFICATIONS_QUEUE_URL", "")
    if not queue_url:
        logger.warning("NOTIFICATIONS_QUEUE_URL no configurado — notificación omitida", extra={"tipo": tipo})
        return
    try:
        _sqs().send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps({"tipo": tipo, "params": params}, separators=(",", ":")),
        )
        logger.info("Notificación encolada", extra={"tipo": tipo})
    except ClientError as e:
        logger.error("Error encolando notificación SQS", extra={"error": str(e), "tipo": tipo})


def encolar_nueva_solicitud(
    admin_emails: list[str],
    company_name: str,
    rut: str,
    contact_name: str,
    contact_email: str,
    request_id: str,
) -> None:
    """Encola notificación de nueva solicitud de empresa para los admins."""
    _encolar(
        "nueva_solicitud",
        {
            "admin_emails": admin_emails,
            "company_name": company_name,
            "rut": rut,
            "contact_name": contact_name,
            "contact_email": contact_email,
            "request_id": request_id,
        },
    )


def encolar_aprobacion(
    contact_email: str,
    contact_name: str,
    company_name: str,
) -> None:
    """Encola notificación de aprobación de solicitud de empresa."""
    _encolar(
        "solicitud_aprobada",
        {
            "contact_email": contact_email,
            "contact_name": contact_name,
            "company_name": company_name,
        },
    )


def encolar_rechazo(
    contact_email: str,
    contact_name: str,
    company_name: str,
    reason: str | None,
) -> None:
    """Encola notificación de rechazo de solicitud de empresa."""
    _encolar(
        "solicitud_rechazada",
        {
            "contact_email": contact_email,
            "contact_name": contact_name,
            "company_name": company_name,
            "reason": reason,
        },
    )
