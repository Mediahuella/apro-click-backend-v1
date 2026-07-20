"""Envío de emails vía AWS SES — utilidad compartida entre servicios."""
from __future__ import annotations

import logging
import os
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


LOGO_URL = "https://cdn.shopify.com/s/files/1/0940/5399/0584/files/Logo_AProClick-Black_48.png?v=1778514883"
ADMIN_PANEL_URL = "https://admin.aproclick.cl"
BRAND_PRIMARY = "#0B5FFF"
BRAND_DARK = "#0A1A2F"
BG_PAGE = "#F4F6FB"
BG_CARD = "#FFFFFF"
TEXT_BODY = "#2B3445"
TEXT_MUTED = "#6B7280"
BORDER = "#E5E7EB"


@lru_cache(maxsize=1)
def _ses():
    region = os.environ.get("SES_REGION", "us-east-1")
    return boto3.client("ses", region_name=region)


def _layout(preheader: str, content_html: str, cta: tuple[str, str] | None = None) -> str:
    """Envuelve el contenido en un layout HTML compatible con clientes de email.

    `preheader` es el texto que se muestra en la vista previa del inbox.
    `cta` es una tupla opcional (texto_boton, url).
    """
    cta_html = ""
    if cta:
        cta_text, cta_url = cta
        cta_html = f"""
        <tr>
          <td align="center" style="padding:24px 0 8px 0;">
            <a href="{cta_url}"
               style="display:inline-block;background:{BRAND_PRIMARY};color:#FFFFFF;
                      text-decoration:none;font-weight:600;font-size:15px;
                      padding:14px 28px;border-radius:8px;
                      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">
              {cta_text}
            </a>
          </td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Apro Click</title>
</head>
<body style="margin:0;padding:0;background:{BG_PAGE};
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;
             color:{TEXT_BODY};">
  <span style="display:none !important;visibility:hidden;opacity:0;color:transparent;
               height:0;width:0;overflow:hidden;mso-hide:all;">
    {preheader}
  </span>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
         style="background:{BG_PAGE};padding:32px 12px;">
    <tr>
      <td align="center">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
               style="max-width:560px;background:{BG_CARD};border:1px solid {BORDER};
                      border-radius:14px;overflow:hidden;">
          <tr>
            <td align="center" style="padding:28px 24px 8px 24px;background:{BG_CARD};">
              <img src="{LOGO_URL}" alt="Apro Click" height="36"
                   style="display:block;border:0;outline:none;text-decoration:none;
                          height:36px;width:auto;" />
            </td>
          </tr>
          <tr>
            <td style="padding:8px 32px 24px 32px;font-size:15px;line-height:1.6;color:{TEXT_BODY};">
              {content_html}
            </td>
          </tr>
          {cta_html}
          <tr>
            <td style="padding:24px 32px 28px 32px;border-top:1px solid {BORDER};
                       font-size:12px;color:{TEXT_MUTED};line-height:1.5;text-align:center;">
              Recibiste este correo porque tu cuenta o solicitud está vinculada a
              <a href="https://www.aproclick.cl"
                 style="color:{BRAND_PRIMARY};text-decoration:none;">Apro Click</a>.<br />
              © Apro Click — Distribución B2B
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _enviar(to: list[str], subject: str, html: str) -> None:
    """Envía vía SES. Non-blocking: loguea el error si falla."""
    from_email = os.environ.get("SES_FROM_EMAIL", "")
    if not from_email or not to:
        logger.warning("SES_FROM_EMAIL no configurado o lista de destinatarios vacía")
        return
    try:
        _ses().send_email(
            Source=f"Apro Click <{from_email}>" if "<" not in from_email else from_email,
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
    preheader = f"{company_name} ({rut}) solicitó acceso B2B en Apro Click."
    content = f"""
      <h1 style="margin:0 0 12px 0;font-size:20px;color:{BRAND_DARK};">
        Nueva solicitud de empresa
      </h1>
      <p style="margin:0 0 20px 0;color:{TEXT_MUTED};">
        Una empresa solicitó acceso al portal B2B y está esperando revisión.
      </p>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
             style="background:{BG_PAGE};border-radius:10px;padding:16px;">
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Empresa</td>
            <td style="padding:6px 4px;font-weight:600;">{company_name}</td></tr>
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">RUT</td>
            <td style="padding:6px 4px;font-weight:600;">{rut}</td></tr>
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Contacto</td>
            <td style="padding:6px 4px;">{contact_name}</td></tr>
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Email</td>
            <td style="padding:6px 4px;">
              <a href="mailto:{contact_email}" style="color:{BRAND_PRIMARY};text-decoration:none;">
                {contact_email}
              </a>
            </td></tr>
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">ID solicitud</td>
            <td style="padding:6px 4px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;">
              {request_id}
            </td></tr>
      </table>
    """
    html = _layout(
        preheader=preheader,
        content_html=content,
        cta=("Revisar en el panel", f"{ADMIN_PANEL_URL}/solicitudes-empresa"),
    )
    _enviar(admin_emails, subject, html)


def notify_approval(
    contact_email: str,
    contact_name: str,
    company_name: str,
) -> None:
    """Notifica al solicitante que su empresa fue aprobada."""
    subject = "Tu solicitud de empresa fue aprobada — Apro Click"
    preheader = f"La solicitud de {company_name} fue aprobada. Ya puedes acceder."
    content = f"""
      <h1 style="margin:0 0 12px 0;font-size:20px;color:{BRAND_DARK};">
        ¡Bienvenido/a a Apro Click!
      </h1>
      <p style="margin:0 0 14px 0;">Hola {contact_name},</p>
      <p style="margin:0 0 14px 0;">
        Tu cuenta en Apro Click está <b>habilitada</b>. Ya puedes ingresar a la tienda
        y acceder a los precios y condiciones B2B disponibles para
        <b style="color:{BRAND_DARK};">{company_name}</b>.
      </p>
    """
    html = _layout(
        preheader=preheader,
        content_html=content,
        cta=("Ir a la tienda", "https://www.aproclick.cl"),
    )
    _enviar([contact_email], subject, html)


def notify_rejection(
    contact_email: str,
    contact_name: str,
    company_name: str,
    reason: str | None,
) -> None:
    """Notifica al solicitante que su solicitud fue rechazada."""
    subject = "Actualización sobre tu solicitud de empresa — Apro Click"
    preheader = f"Revisamos la solicitud de {company_name}."
    reason_block = ""
    if reason:
        reason_block = f"""
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
               style="background:{BG_PAGE};border-left:3px solid {BRAND_PRIMARY};
                      border-radius:6px;padding:12px 16px;margin:12px 0;">
          <tr><td style="color:{TEXT_MUTED};font-size:13px;padding-bottom:4px;">Motivo</td></tr>
          <tr><td style="color:{TEXT_BODY};">{reason}</td></tr>
        </table>
        """
    content = f"""
      <h1 style="margin:0 0 12px 0;font-size:20px;color:{BRAND_DARK};">
        Solicitud de empresa revisada
      </h1>
      <p style="margin:0 0 14px 0;">Hola {contact_name},</p>
      <p style="margin:0 0 14px 0;">
        Hemos revisado la solicitud de
        <b style="color:{BRAND_DARK};">{company_name}</b> y lamentablemente
        no ha podido ser aprobada en este momento.
      </p>
      {reason_block}
      <p style="margin:18px 0 0 0;color:{TEXT_MUTED};font-size:13px;">
        Si tienes preguntas o deseas entregar más información,
        no dudes en contactarnos respondiendo este correo.
      </p>
    """
    html = _layout(preheader=preheader, content_html=content)
    _enviar([contact_email], subject, html)


def notify_confirmacion_solicitud(
    contact_email: str,
    contact_name: str | None,
    company_name: str,
    quote_id: str,
    lines: list[dict],
) -> None:
    """Notifica al cliente que su solicitud de cotización fue recibida."""
    if not contact_email:
        return
    subject = f"Recibimos tu solicitud de cotización — Apro Click"
    preheader = f"Tu solicitud de cotización para {company_name} fue recibida correctamente."
    filas = ""
    for ln in (lines or [])[:20]:
        titulo = ln.get("title") or ln.get("sku") or "-"
        qty = ln.get("quantity") or 1
        filas += f"""
        <tr>
          <td style="padding:6px 8px;border-bottom:1px solid {BORDER};font-size:13px;">{titulo}</td>
          <td style="padding:6px 8px;border-bottom:1px solid {BORDER};font-size:13px;text-align:center;">{qty}</td>
        </tr>
        """
    content = f"""
      <h1 style="margin:0 0 12px 0;font-size:20px;color:{BRAND_DARK};">
        Recibimos tu solicitud
      </h1>
      <p style="margin:0 0 14px 0;">Hola {contact_name or ''},</p>
      <p style="margin:0 0 14px 0;">
        Recibimos tu solicitud de cotización para <b style="color:{BRAND_DARK};">{company_name}</b>.
        Nuestro equipo la revisará y te enviaremos la cotización formal con los precios a la brevedad.
      </p>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
             style="background:{BG_PAGE};border-radius:10px;margin:16px 0;">
        <tr>
          <th style="padding:8px 8px;text-align:left;color:{TEXT_MUTED};font-size:12px;font-weight:600;
                     border-bottom:1px solid {BORDER};">PRODUCTO</th>
          <th style="padding:8px 8px;text-align:center;color:{TEXT_MUTED};font-size:12px;font-weight:600;
                     border-bottom:1px solid {BORDER};">CANT.</th>
        </tr>
        {filas}
      </table>
      <p style="margin:18px 0 0 0;color:{TEXT_MUTED};font-size:13px;">
        Si tienes alguna consulta, responde este correo y con gusto te ayudamos.
      </p>
    """
    html = _layout(
        preheader=preheader,
        content_html=content,
        cta=("Ver mis cotizaciones", "https://www.aproclick.cl"),
    )
    _enviar([contact_email], subject, html)


def notify_nueva_cotizacion(
    admin_emails: list[str],
    company_name: str,
    quote_id: str,
    line_count: int,
    contact_name: str | None = None,
    contact_email: str | None = None,
) -> None:
    """Notifica a SALES/admins que llegó una nueva solicitud de cotización."""
    if not admin_emails:
        return
    subject = f"Nueva solicitud de cotización: {company_name}"
    preheader = f"{company_name} solicitó una cotización ({line_count} productos)."
    contacto = ""
    if contact_name or contact_email:
        contacto = f"""
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Contacto</td>
            <td style="padding:6px 4px;">{contact_name or '-'}</td></tr>
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Email</td>
            <td style="padding:6px 4px;">{contact_email or '-'}</td></tr>
        """
    content = f"""
      <h1 style="margin:0 0 12px 0;font-size:20px;color:{BRAND_DARK};">
        Nueva solicitud de cotización
      </h1>
      <p style="margin:0 0 20px 0;color:{TEXT_MUTED};">
        Una empresa solicitó una cotización y está esperando revisión de precios.
      </p>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
             style="background:{BG_PAGE};border-radius:10px;padding:16px;">
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Empresa</td>
            <td style="padding:6px 4px;font-weight:600;">{company_name}</td></tr>
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Productos</td>
            <td style="padding:6px 4px;font-weight:600;">{line_count}</td></tr>
        {contacto}
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">ID</td>
            <td style="padding:6px 4px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;">
              {quote_id}
            </td></tr>
      </table>
    """
    html = _layout(
        preheader=preheader,
        content_html=content,
        cta=("Revisar en el panel", f"{ADMIN_PANEL_URL}/quotes"),
    )
    _enviar(admin_emails, subject, html)


def notify_cotizacion_lista(
    contact_email: str,
    contact_name: str | None,
    company_name: str,
    folio: str,
    pdf_url: str,
    total: str | None = None,
) -> None:
    """Notifica al cliente que su cotización está lista, con link de descarga del PDF."""
    if not contact_email:
        return
    subject = f"Tu cotización {folio} está lista — Apro Click"
    preheader = f"Cotización {folio} para {company_name} disponible para descargar."
    total_block = ""
    if total:
        total_block = f"""
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Total</td>
            <td style="padding:6px 4px;font-weight:600;">{total}</td></tr>
        """
    content = f"""
      <h1 style="margin:0 0 12px 0;font-size:20px;color:{BRAND_DARK};">
        Tu cotización está lista
      </h1>
      <p style="margin:0 0 14px 0;">Hola {contact_name or ''},</p>
      <p style="margin:0 0 14px 0;">
        Preparamos la cotización <b style="color:{BRAND_DARK};">{folio}</b> para
        <b>{company_name}</b>. Puedes descargarla en PDF con el botón de abajo.
      </p>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
             style="background:{BG_PAGE};border-radius:10px;padding:16px;">
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Folio</td>
            <td style="padding:6px 4px;font-weight:600;">{folio}</td></tr>
        {total_block}
      </table>
      <p style="margin:18px 0 0 0;color:{TEXT_MUTED};font-size:13px;">
        Esta cotización tiene una validez limitada. Haz referencia al folio en tu orden de compra.
      </p>
    """
    html = _layout(
        preheader=preheader,
        content_html=content,
        cta=("Descargar cotización (PDF)", pdf_url),
    )
    _enviar([contact_email], subject, html)


def notify_cotizacion_apro_ia_estado(
    admin_emails: list[str],
    company_name: str,
    folio: str,
    quote_id: str,
    event: str,
    status: str,
) -> None:
    """Notifica a SALES/admins que APRO IA autorizó o rechazó una cotización."""
    if not admin_emails:
        return
    aprobada = event == "quotation.approved"
    subject = (
        f"Cotización {folio} autorizada por APRO IA"
        if aprobada
        else f"Cotización {folio} rechazada por APRO IA"
    )
    preheader = f"{company_name} — {folio}: {status}"
    accent = "#1a7f37" if aprobada else "#b42318"
    content = f"""
      <h1 style="margin:0 0 12px 0;font-size:20px;color:{BRAND_DARK};">
        {'Cotización autorizada' if aprobada else 'Cotización rechazada'} en APRO IA
      </h1>
      <p style="margin:0 0 20px 0;color:{TEXT_MUTED};">
        El Gerente Comercial {'autorizó' if aprobada else 'rechazó'} esta cotización en el
        DMS de APRO IA.
      </p>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
             style="background:{BG_PAGE};border-radius:10px;padding:16px;">
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Empresa</td>
            <td style="padding:6px 4px;font-weight:600;">{company_name}</td></tr>
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Folio</td>
            <td style="padding:6px 4px;font-weight:600;">{folio}</td></tr>
        <tr><td style="padding:6px 4px;color:{TEXT_MUTED};font-size:13px;">Estado APRO IA</td>
            <td style="padding:6px 4px;font-weight:600;color:{accent};">{status}</td></tr>
      </table>
    """
    html = _layout(
        preheader=preheader,
        content_html=content,
        cta=("Revisar en el panel", f"{ADMIN_PANEL_URL}/quotes/{quote_id}"),
    )
    _enviar(admin_emails, subject, html)
