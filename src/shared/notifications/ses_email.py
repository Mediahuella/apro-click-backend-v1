"""Envío de emails vía AWS SES — utilidad compartida entre servicios."""
from __future__ import annotations

import logging
import os
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _ses():
    region = os.environ.get("SES_REGION", "us-east-1")
    return boto3.client("ses", region_name=region)


def _enviar(to: list[str], subject: str, html: str) -> None:
    """Envía vía SES. Non-blocking: loguea el error si falla."""
    from_email = os.environ.get("SES_FROM_EMAIL", "")
    if not from_email or not to:
        logger.warning("SES_FROM_EMAIL no configurado o lista de destinatarios vacía")
        return
    try:
        _ses().send_email(
            Source=from_email,
            Destination={"ToAddresses": to},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": html, "Charset": "UTF-8"}},
            },
        )
        logger.info("Email enviado", extra={"to": to, "subject": subject})
    except ClientError as e:
        logger.error("Error enviando email SES", extra={"error": str(e), "to": to})


def notify_new_request(
    admin_emails: list[str],
    company_name: str,
    rut: str,
    contact_name: str,
    contact_email: str,
    request_id: str,
) -> None:
    """Notifica a los admins activos que llegó una nueva solicitud de empresa."""
    if not admin_emails:
        return
    subject = f"Nueva solicitud de empresa: {company_name}"
    html = f"""
    <h2>Nueva solicitud de empresa</h2>
    <p><b>Empresa:</b> {company_name}</p>
    <p><b>RUT:</b> {rut}</p>
    <p><b>Contacto:</b> {contact_name} ({contact_email})</p>
    <p><b>ID solicitud:</b> <code>{request_id}</code></p>
    <p>
      <a href="https://admin.aproclick.cl/solicitudes-empresa">
        Ver en el panel de administración &rarr;
      </a>
    </p>
    """
    _enviar(admin_emails, subject, html)


def notify_approval(
    contact_email: str,
    contact_name: str,
    company_name: str,
) -> None:
    """Notifica al solicitante que su empresa fue aprobada."""
    subject = "Tu solicitud de empresa fue aprobada — Apro Click"
    html = f"""
    <h2>¡Bienvenido/a a Apro Click!</h2>
    <p>Hola {contact_name},</p>
    <p>
      Nos complace informarte que la solicitud de la empresa
      <b>{company_name}</b> ha sido <b>aprobada</b>.
    </p>
    <p>
      Ya puedes acceder a la tienda con tu cuenta y ver los precios
      y condiciones B2B disponibles para tu empresa.
    </p>
    <p>Si tienes dudas, responde este correo y con gusto te ayudamos.</p>
    """
    _enviar([contact_email], subject, html)


def notify_rejection(
    contact_email: str,
    contact_name: str,
    company_name: str,
    reason: str | None,
) -> None:
    """Notifica al solicitante que su solicitud fue rechazada."""
    subject = "Actualización sobre tu solicitud de empresa — Apro Click"
    reason_html = f"<p><b>Motivo:</b> {reason}</p>" if reason else ""
    html = f"""
    <h2>Solicitud de empresa revisada</h2>
    <p>Hola {contact_name},</p>
    <p>
      Hemos revisado la solicitud de <b>{company_name}</b> y lamentablemente
      no ha podido ser aprobada en este momento.
    </p>
    {reason_html}
    <p>
      Si tienes preguntas o deseas entregar más información,
      no dudes en contactarnos respondiendo este correo.
    </p>
    """
    _enviar([contact_email], subject, html)
