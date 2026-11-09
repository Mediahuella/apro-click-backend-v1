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
    seller_name: str | None = None,
    credit_url: str | None = None,
) -> None:
    """Encola notificación de aprobación de solicitud de empresa.

    ``seller_name`` es el vendedor asignado y ``credit_url`` el link de APRO
    para solicitar crédito (solo si la empresa quedó sin crédito).
    """
    _encolar(
        "solicitud_aprobada",
        {
            "contact_email": contact_email,
            "contact_name": contact_name,
            "company_name": company_name,
            "seller_name": seller_name,
            "credit_url": credit_url,
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


def encolar_nueva_cotizacion(
    admin_emails: list[str],
    company_name: str,
    quote_id: str,
    line_count: int,
    contact_name: str | None = None,
    contact_email: str | None = None,
) -> None:
    """Encola aviso a SALES/admins de una nueva solicitud de cotización."""
    _encolar(
        "nueva_cotizacion",
        {
            "admin_emails": admin_emails,
            "company_name": company_name,
            "quote_id": quote_id,
            "line_count": line_count,
            "contact_name": contact_name,
            "contact_email": contact_email,
        },
    )


def encolar_confirmacion_solicitud(
    contact_email: str,
    contact_name: str | None,
    company_name: str,
    quote_id: str,
    lines: list[dict],
) -> None:
    """Encola confirmación al cliente de que su solicitud de cotización fue recibida."""
    _encolar(
        "confirmacion_solicitud",
        {
            "contact_email": contact_email,
            "contact_name": contact_name,
            "company_name": company_name,
            "quote_id": quote_id,
            "lines": lines,
        },
    )


def encolar_cotizacion_lista(
    contact_email: str,
    contact_name: str | None,
    company_name: str,
    folio: str,
    pdf_url: str,
    total: str | None = None,
) -> None:
    """Encola aviso al cliente de que su cotización está lista (con link al PDF)."""
    _encolar(
        "cotizacion_lista",
        {
            "contact_email": contact_email,
            "contact_name": contact_name,
            "company_name": company_name,
            "folio": folio,
            "pdf_url": pdf_url,
            "total": total,
        },
    )


def encolar_cotizacion_apro_ia_estado(
    admin_emails: list[str],
    company_name: str,
    folio: str,
    quote_id: str,
    event: str,
    status: str,
) -> None:
    """Encola aviso a SALES/admins de un cambio de estado reportado por el webhook APRO IA."""
    _encolar(
        "cotizacion_apro_ia_estado",
        {
            "admin_emails": admin_emails,
            "company_name": company_name,
            "folio": folio,
            "quote_id": quote_id,
            "event": event,
            "status": status,
        },
    )


def encolar_transferencia_estado(
    admin_emails: list[str],
    company_name: str,
    folio: str,
    quote_id: str,
    transfer_folio: str,
    estado: str,
    status: str,
    payment_method: str = "transferencia",
) -> None:
    """Encola aviso a SALES/admins del resultado de un pago reportado por APRO IA.

    ``estado`` es el normalizado (``APROBADA``/``ANULADA``) y ``status`` el
    texto crudo que envió el DMS. ``payment_method`` (``transferencia`` o
    ``credito``) solo cambia los textos del correo.
    """
    _encolar(
        "transferencia_estado",
        {
            "admin_emails": admin_emails,
            "company_name": company_name,
            "folio": folio,
            "quote_id": quote_id,
            "transfer_folio": transfer_folio,
            "estado": estado,
            "status": status,
            "payment_method": payment_method,
        },
    )


def encolar_pago_credito_cliente(
    contact_email: str,
    contact_name: str | None,
    company_name: str,
    order_name: str,
    aprobado: bool,
    total: str | None = None,
) -> None:
    """Encola el aviso al cliente de que su pago con Crédito Apro se aprobó o rechazó."""
    _encolar(
        "pago_credito_cliente",
        {
            "contact_email": contact_email,
            "contact_name": contact_name,
            "company_name": company_name,
            "order_name": order_name,
            "aprobado": aprobado,
            "total": total,
        },
    )


def encolar_credito_aprobado(
    contact_email: str,
    contact_name: str | None,
    company_name: str,
) -> None:
    """Encola el aviso al cliente de que APRO aprobó el crédito de su empresa."""
    _encolar(
        "credito_aprobado",
        {
            "contact_email": contact_email,
            "contact_name": contact_name,
            "company_name": company_name,
        },
    )
