#!/usr/bin/env python3
"""Campaña: aviso por email a las empresas que ya tienen Crédito Apro activo.

Destinatarios: los contactos (`clients` con email) de las empresas con
`payment_type = 'CREDIT'`, sin repetir email dentro de una misma empresa.

Seguimiento, en dos niveles:
  - **Por destinatario** (entregas, aperturas, clics, rebotes): cada correo sale
    con el configuration set de SES `aproclick-campanas`, que manda los eventos
    por EventBridge al log group `/aws/events/aproclick-email-campanas`. Cada
    correo lleva los tags `campana` y `company_id` para filtrar.
  - **En Shopify** (visitas y ventas): el botón lleva `utm_source=email`,
    `utm_medium=email`, `utm_campaign=credito-apro-activo`.

Ver consultas de ejemplo en docs/CAMPANA_CREDITO_APRO.md.

Uso:
  python3 scripts/campana_credito_apro.py                         # dry-run: lista destinatarios
  python3 scripts/campana_credito_apro.py --test-to yo@dominio.cl # un ejemplo con datos de prueba
  python3 scripts/campana_credito_apro.py --apply                 # envía a todos

Con --apply se registra cada envío en `--registro` (CSV) y en una segunda
corrida se saltan los emails ya enviados, así que se puede reanudar sin
duplicar correos.

Variables de entorno (desde .env raíz o shell):
  DATABASE_URL, SES_FROM_EMAIL, SES_REGION (default us-east-1)
  CAMPANAS_SES_CONFIGURATION_SET — opcional, default `aproclick-campanas`
  Credenciales AWS de la cuenta productiva (p. ej. AWS_PROFILE=mh-prod)
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

_SCRIPTS = Path(__file__).resolve().parent
_ROOT = _SCRIPTS.parent

sys.path.insert(0, str(_SCRIPTS))
from dotenv_loader import load_root_dotenv_defaults  # noqa: E402

load_root_dotenv_defaults(_ROOT)

_SHARED = _ROOT / "src" / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

import os  # noqa: E402

from botocore.exceptions import ClientError  # noqa: E402
from sqlalchemy import select  # noqa: E402

import notifications.ses_email as ses  # noqa: E402
from database.engine import get_session  # noqa: E402
from database.models.client import Client  # noqa: E402
from database.models.company import Company  # noqa: E402

CAMPANA = "credito-apro-activo"
TIENDA_URL = "https://www.aproclick.cl/"
ASUNTO = "Tu empresa ya puede comprar con Crédito Apro"

#: SES permite 14 envíos por segundo en esta cuenta; se deja margen.
_PAUSA_ENTRE_ENVIOS = 0.1


def _url_tienda() -> str:
    utm = {"utm_source": "email", "utm_medium": "email", "utm_campaign": CAMPANA}
    return f"{TIENDA_URL}?{urlencode(utm)}"


def construir_email(nombre_contacto: str | None, nombre_empresa: str) -> tuple[str, str]:
    """Devuelve (asunto, html)."""
    saludo = f"Hola {nombre_contacto}," if nombre_contacto else "Hola,"
    paso = "padding:6px 0;vertical-align:top;"
    numero = (
        f"display:inline-block;width:24px;height:24px;line-height:24px;"
        f"border-radius:12px;background:{ses.BRAND_BUTTON};color:#FFFFFF;"
        f"font-size:13px;font-weight:600;text-align:center;"
    )
    content = f"""
      <h1 style="margin:0 0 12px 0;font-size:22px;color:{ses.BRAND_DARK};">
        Ya puedes comprar con Crédito Apro
      </h1>
      <p style="margin:0 0 14px 0;">{saludo}</p>
      <p style="margin:0 0 14px 0;">
        Te contamos que <b style="color:{ses.BRAND_DARK};">{nombre_empresa}</b> tiene
        <b>crédito Apro activo</b>. Desde ahora puedes pagar tus pedidos en Apro Click
        con el método de pago <b>«Crédito Apro»</b>, directamente en el checkout.
      </p>
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
             style="background:{ses.BG_PAGE};border-radius:10px;padding:16px 18px;margin:6px 0 4px 0;">
        <tr><td colspan="2" style="padding:0 0 8px 0;font-weight:600;color:{ses.BRAND_DARK};">
          ¿Cómo usarlo?
        </td></tr>
        <tr><td width="36" style="{paso}"><span style="{numero}">1</span></td>
            <td style="{paso}">Inicia sesión en Apro Click con tu cuenta de empresa.</td></tr>
        <tr><td width="36" style="{paso}"><span style="{numero}">2</span></td>
            <td style="{paso}">Agrega los productos que necesitas al carrito.</td></tr>
        <tr><td width="36" style="{paso}"><span style="{numero}">3</span></td>
            <td style="{paso}">En el checkout, selecciona <b>«Crédito Apro»</b> como método de pago.</td></tr>
      </table>
      <p style="margin:16px 0 0 0;color:{ses.TEXT_MUTED};font-size:13px;">
        Las condiciones de tu crédito son las acordadas con Apro. Si tienes dudas,
        escríbele a tu ejecutivo comercial.
      </p>
    """
    html = ses._layout(
        preheader=f"{nombre_empresa} ya puede pagar sus pedidos con Crédito Apro en Apro Click.",
        content_html=content,
        cta=("Comprar en Apro Click", _url_tienda()),
        portal_apro=True,
    )
    return ASUNTO, html


def _tag(valor: str) -> str:
    """SES solo acepta letras, números, `_` y `-` en los valores de los tags."""
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in valor)[:256] or "na"


def enviar(to: str, asunto: str, html: str, company_id: str) -> str:
    """Envía por SES con el configuration set de campañas. Devuelve el MessageId."""
    from_email = os.environ["SES_FROM_EMAIL"]
    respuesta = ses._ses().send_email(
        Source=f"Apro Click <{from_email}>" if "<" not in from_email else from_email,
        Destination={"ToAddresses": [to]},
        Message={
            "Subject": {"Data": asunto, "Charset": "UTF-8"},
            "Body": {"Html": {"Data": html, "Charset": "UTF-8"}},
        },
        ConfigurationSetName=os.environ.get(
            "CAMPANAS_SES_CONFIGURATION_SET", "aproclick-campanas"
        ),
        Tags=[
            {"Name": "campana", "Value": CAMPANA},
            {"Name": "company_id", "Value": _tag(company_id)},
        ],
    )
    return respuesta["MessageId"]


def destinatarios() -> list[dict]:
    with get_session() as session:
        filas = session.execute(
            select(Company.id, Company.name, Client.name, Client.email)
            .join(Client, Client.company_id == Company.id)
            .where(Company.payment_type == "CREDIT")
            .where(Company.is_system.is_(False))
            .where(Client.email.is_not(None))
            .order_by(Company.name, Client.is_main_contact.desc())
        ).all()
    vistos: set[tuple[str, str]] = set()
    salida: list[dict] = []
    for company_id, empresa, contacto, email in filas:
        email = (email or "").strip().lower()
        clave = (str(company_id), email)
        if not email or clave in vistos:
            continue
        vistos.add(clave)
        salida.append(
            {"company_id": str(company_id), "empresa": empresa, "contacto": contacto, "email": email}
        )
    return salida


def _ya_enviados(registro: Path) -> set[tuple[str, str]]:
    if not registro.exists():
        return set()
    with registro.open(newline="") as f:
        return {(r["company_id"], r["email"]) for r in csv.DictReader(f)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    grupo = parser.add_mutually_exclusive_group()
    grupo.add_argument("--test-to", metavar="EMAIL", help="envía un ejemplo con datos de prueba")
    grupo.add_argument("--apply", action="store_true", help="envía a todos los destinatarios")
    parser.add_argument(
        "--registro",
        type=Path,
        default=Path("campana_credito_apro_enviados.csv"),
        help="CSV de envíos realizados (default: %(default)s)",
    )
    args = parser.parse_args()

    if args.test_to:
        asunto, html = construir_email("Sebastián", "EMPRESA DE PRUEBA SPA")
        message_id = enviar(args.test_to, f"[Prueba] {asunto}", html, "prueba")
        print(f"Ejemplo enviado a {args.test_to} (MessageId {message_id})")
        return 0

    lista = destinatarios()
    enviados = _ya_enviados(args.registro)
    pendientes = [d for d in lista if (d["company_id"], d["email"]) not in enviados]
    print(
        f"{len(lista)} destinatarios en {len({d['company_id'] for d in lista})} empresas con crédito; "
        f"{len(lista) - len(pendientes)} ya enviados, {len(pendientes)} pendientes."
    )

    if not args.apply:
        for d in pendientes:
            print(f"  {d['empresa']:<45} {d['email']}")
        print("\nDry-run: no se envió nada. Usa --apply para enviar.")
        return 0

    nuevo = not args.registro.exists()
    errores = 0
    with args.registro.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["enviado_at", "company_id", "empresa", "email", "message_id"])
        if nuevo:
            writer.writeheader()
        for d in pendientes:
            asunto, html = construir_email(d["contacto"], d["empresa"])
            try:
                message_id = enviar(d["email"], asunto, html, d["company_id"])
            except ClientError as e:
                errores += 1
                print(f"  ERROR {d['email']}: {e}")
                continue
            writer.writerow(
                {
                    "enviado_at": datetime.now(timezone.utc).isoformat(),
                    "company_id": d["company_id"],
                    "empresa": d["empresa"],
                    "email": d["email"],
                    "message_id": message_id,
                }
            )
            f.flush()
            print(f"  ok {d['email']}")
            time.sleep(_PAUSA_ENTRE_ENVIOS)

    print(f"\nListo: {len(pendientes) - errores} enviados, {errores} con error. Registro en {args.registro}")
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())
