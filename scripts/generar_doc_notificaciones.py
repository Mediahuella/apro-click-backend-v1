#!/usr/bin/env python3
"""Genera `docs/notificaciones-email.html`: el catálogo de correos para el equipo.

Las vistas previas **no** están escritas a mano: se obtienen llamando a las
mismas funciones de `shared/notifications/ses_email.py` que usa el worker, con
`_enviar` interceptado para capturar el HTML en vez de mandarlo por SES. Así el
documento no puede quedar desactualizado respecto del correo real — si cambia
la plantilla, se regenera y listo.

Uso:
  python3 scripts/generar_doc_notificaciones.py

No necesita credenciales AWS ni base de datos: solo importa el módulo y captura.
"""
from __future__ import annotations

import html as html_mod
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT / "src" / "shared"))

import notifications.ses_email as ses  # noqa: E402

SALIDA = REPO / "docs" / "notificaciones-email.html"
ESTILOS = Path(__file__).resolve().parent / "assets" / "notificaciones_doc.css"

# Datos de muestra: ficticios a propósito — el documento circula fuera del
# equipo técnico y no debe llevar datos de clientes reales.
EMPRESA = "COMERCIAL EJEMPLO SPA"
RUT = "76.123.456-7"
CONTACTO = "Ana Riquelme"
EMAIL = "ana.riquelme@comercialejemplo.cl"
ADMINS = ["ventas@apro.cl", "admin@apro.cl"]


def _capturar(fn, /, **kwargs) -> tuple[list[str], str, str]:
    """Llama a una función de notificación y devuelve (destinatarios, asunto, html)."""
    capturado: list[tuple[list[str], str, str]] = []
    original = ses._enviar
    ses._enviar = lambda to, subject, html: capturado.append((to, subject, html))
    try:
        fn(**kwargs)
    finally:
        ses._enviar = original
    if not capturado:
        raise RuntimeError(f"{fn.__name__} no envió nada con los datos de muestra")
    return capturado[0]


def correos() -> list[dict]:
    """Las once notificaciones, en el orden de los tres flujos de negocio."""
    return [
        {
            "grupo": "Alta de empresa",
            "titulo": "Nueva solicitud de empresa",
            "clave": "nueva_solicitud",
            "publico": "equipo",
            "disparador": "Una empresa envía el formulario de registro B2B desde la tienda.",
            "destinatarios": "Todos los SUPERADMIN y ADMIN con cuenta activa.",
            "boton": "Revisar en el panel",
            "cuerpo": "Empresa, RUT, nombre y correo del contacto, e ID de la solicitud.",
            "nota": None,
            "captura": _capturar(
                ses.notify_new_request,
                admin_emails=ADMINS,
                company_name=EMPRESA,
                rut=RUT,
                contact_name=CONTACTO,
                contact_email=EMAIL,
                request_id="8f2c41ab-5d70-4e19-9c33-6a1b2e84d5f0",
            ),
        },
        {
            "grupo": "Alta de empresa",
            "titulo": "Solicitud aprobada",
            "clave": "solicitud_aprobada",
            "publico": "cliente",
            "disparador": "Un admin aprueba la solicitud. Sale después de crear la empresa en Shopify.",
            "destinatarios": "El contacto que hizo el registro.",
            "boton": "Ir a la tienda",
            "cuerpo": "Bienvenida con el nombre del contacto y de la empresa, el aviso de que ya puede ver precios B2B, el vendedor asignado y, si la empresa quedó al contado, el botón «Solicitar crédito» con el link de APRO.",
            "nota": "El botón lleva un link estable de Apro Click que, al hacer clic, pide a APRO un link recién generado (con el código SAP del vendedor) y redirige: APRO invalida el link anterior cada vez que entrega otro. Si la empresa ya tiene crédito en APRO, el bloque de crédito no aparece; sin vendedor asignado tampoco aparece su nombre.",
            "captura": _capturar(
                ses.notify_approval,
                contact_email=EMAIL,
                contact_name=CONTACTO,
                company_name=EMPRESA,
                seller_name="Bryan Ramírez",
                credit_url="https://apm2ytw49j.execute-api.us-east-2.amazonaws.com/api/v1/storefront/company/credit/go?t=ejemplo",
            ),
        },
        {
            "grupo": "Alta de empresa",
            "titulo": "Solicitud rechazada",
            "clave": "solicitud_rechazada",
            "publico": "cliente",
            "disparador": "Un admin rechaza la solicitud en el panel.",
            "destinatarios": "El contacto que hizo el registro.",
            "boton": None,
            "cuerpo": "Aviso de que no pudo aprobarse e invitación a responder. Si al rechazar se escribe un motivo, se muestra al cliente.",
            "nota": "El asunto no dice «rechazada» a propósito: el motivo se explica adentro.",
            "captura": _capturar(
                ses.notify_rejection,
                contact_email=EMAIL,
                contact_name=CONTACTO,
                company_name=EMPRESA,
                reason="No pudimos validar el giro comercial informado. Envíanos la carpeta tributaria y retomamos la solicitud.",
            ),
        },
        {
            "grupo": "Cotizaciones y pagos",
            "titulo": "Nueva solicitud de cotización",
            "clave": "nueva_cotizacion",
            "publico": "equipo",
            "disparador": "Un cliente pide cotizar desde el carrito. Se envía siempre, antes de intentar armar la cotización.",
            "destinatarios": "El vendedor asignado a la empresa (si lo tiene y está activo) y todos los SUPERADMIN y ADMIN activos.",
            "boton": "Revisar en el panel",
            "cuerpo": "Empresa, cantidad de productos, contacto que la pidió e ID de la cotización.",
            "nota": None,
            "captura": _capturar(
                ses.notify_nueva_cotizacion,
                admin_emails=ADMINS,
                company_name=EMPRESA,
                quote_id="3a91d55c-2f18-4a76-b0e2-77c4f9a1e802",
                line_count=4,
                contact_name=CONTACTO,
                contact_email=EMAIL,
            ),
        },
        {
            "grupo": "Cotizaciones y pagos",
            "titulo": "Recibimos tu solicitud",
            "clave": "confirmacion_solicitud",
            "publico": "cliente",
            "disparador": "La cotización entró pero no se pudo emitir automáticamente: queda para trabajo manual del equipo.",
            "destinatarios": "El contacto que pidió la cotización.",
            "boton": "Ver mis cotizaciones",
            "cuerpo": "Acuse de recibo con la tabla de productos y cantidades (hasta 20 líneas).",
            "nota": "Este correo y «Tu cotización está lista» son excluyentes: el cliente recibe uno u otro. Si llega este, hay una cotización esperando trabajo manual.",
            "captura": _capturar(
                ses.notify_confirmacion_solicitud,
                contact_email=EMAIL,
                contact_name=CONTACTO,
                company_name=EMPRESA,
                quote_id="3a91d55c-2f18-4a76-b0e2-77c4f9a1e802",
                lines=[
                    {"title": "Guante nitrilo azul talla M — caja 100 un.", "sku": "GN-M-100", "quantity": 12},
                    {"title": "Mascarilla desechable 3 pliegues — caja 50 un.", "sku": "MD-3P-50", "quantity": 30},
                    {"title": "Papel higiénico institucional 500 m", "sku": "PH-500", "quantity": 8},
                    {"title": "Detergente multiuso 5 L", "sku": "DT-5L", "quantity": 6},
                ],
            ),
        },
        {
            "grupo": "Cotizaciones y pagos",
            "titulo": "Tu cotización está lista",
            "clave": "cotizacion_lista",
            "publico": "cliente",
            "disparador": "La cotización quedó emitida con folio y PDF — automáticamente al pedirla, o cuando el equipo la genera desde el panel.",
            "destinatarios": "El contacto que pidió la cotización.",
            "boton": "Descargar cotización (PDF)",
            "cuerpo": "Folio, total y el recordatorio de citar el folio en la orden de compra.",
            "nota": "El link del PDF es personal de esa cotización y va firmado: sirve sin iniciar sesión, así que conviene no reenviarlo fuera del cliente.",
            "captura": _capturar(
                ses.notify_cotizacion_lista,
                contact_email=EMAIL,
                contact_name=CONTACTO,
                company_name=EMPRESA,
                folio="AC-000312",
                pdf_url="https://www.aproclick.cl/cotizacion.pdf",
                total="$1.284.900",
            ),
        },
        {
            "grupo": "Cotizaciones y pagos",
            "titulo": "Autorización de APRO IA",
            "clave": "cotizacion_apro_ia_estado",
            "publico": "equipo",
            "disparador": "El Gerente Comercial autoriza o rechaza la cotización en el DMS de APRO IA.",
            "destinatarios": "El vendedor de la empresa y todos los SUPERADMIN y ADMIN activos.",
            "boton": "Revisar en el panel",
            "cuerpo": "Empresa, folio y el estado tal como lo informa APRO IA — verde si autorizó, rojo si rechazó.",
            "nota": None,
            "captura": _capturar(
                ses.notify_cotizacion_apro_ia_estado,
                admin_emails=ADMINS,
                company_name=EMPRESA,
                folio="AC-000312",
                quote_id="3a91d55c-2f18-4a76-b0e2-77c4f9a1e802",
                event="quotation.approved",
                status="Autorizada",
            ),
            "variante": {
                "etiqueta": "Cuando el resultado es un rechazo",
                "captura": _capturar(
                    ses.notify_cotizacion_apro_ia_estado,
                    admin_emails=ADMINS,
                    company_name=EMPRESA,
                    folio="AC-000312",
                    quote_id="3a91d55c-2f18-4a76-b0e2-77c4f9a1e802",
                    event="quotation.rejected",
                    status="Rechazada",
                ),
            },
        },
        {
            "grupo": "Cotizaciones y pagos",
            "titulo": "Resultado de la transferencia",
            "clave": "transferencia_estado",
            "publico": "equipo",
            "disparador": "APRO IA informa si recibió la transferencia del cliente.",
            "destinatarios": "El vendedor de la empresa y todos los SUPERADMIN y ADMIN activos.",
            "boton": "Revisar en el panel",
            "cuerpo": "Empresa, cotización, folio de la transferencia y estado, más qué pasó con el pedido en Shopify.",
            "nota": "Es el único correo que confirma dinero recibido. Vale la pena leerlo apenas llega.",
            "captura": _capturar(
                ses.notify_transferencia_estado,
                admin_emails=ADMINS,
                company_name=EMPRESA,
                folio="AC-000312",
                quote_id="3a91d55c-2f18-4a76-b0e2-77c4f9a1e802",
                transfer_folio="TR-45821",
                estado="APROBADA",
                status="Aprobada",
            ),
            "variante": {
                "etiqueta": "Cuando la transferencia no se aprueba",
                "captura": _capturar(
                    ses.notify_transferencia_estado,
                    admin_emails=ADMINS,
                    company_name=EMPRESA,
                    folio="AC-000312",
                    quote_id="3a91d55c-2f18-4a76-b0e2-77c4f9a1e802",
                    transfer_folio="TR-45821",
                    estado="DESCARTADA",
                    status="Descartada",
                ),
            },
        },
        {
            "grupo": "Cotizaciones y pagos",
            "titulo": "Resultado del pago con crédito",
            "clave": "transferencia_estado (payment_method=credito)",
            "publico": "equipo",
            "disparador": "APRO IA informa si aprobó el crédito del cliente.",
            "destinatarios": "El vendedor de la empresa y todos los SUPERADMIN y ADMIN activos.",
            "boton": "Revisar en el panel",
            "cuerpo": "Empresa, cotización, folio del crédito y estado, más qué pasó con el pedido en Shopify.",
            "nota": "Mismo correo que el de transferencia: cambia solo el medio de pago que se nombra.",
            "captura": _capturar(
                ses.notify_transferencia_estado,
                admin_emails=ADMINS,
                company_name=EMPRESA,
                folio="AC-000312",
                quote_id="3a91d55c-2f18-4a76-b0e2-77c4f9a1e802",
                transfer_folio="CR-45821",
                estado="APROBADA",
                status="Aprobada",
                payment_method="credito",
            ),
            "variante": {
                "etiqueta": "Cuando el crédito no se aprueba",
                "captura": _capturar(
                    ses.notify_transferencia_estado,
                    admin_emails=ADMINS,
                    company_name=EMPRESA,
                    folio="AC-000312",
                    quote_id="3a91d55c-2f18-4a76-b0e2-77c4f9a1e802",
                    transfer_folio="CR-45821",
                    estado="DESCARTADA",
                    status="Descartada",
                    payment_method="credito",
                ),
            },
        },
        {
            "grupo": "Cotizaciones y pagos",
            "titulo": "Resultado del pago con crédito (al cliente)",
            "clave": "pago_credito_cliente",
            "publico": "cliente",
            "disparador": "APRO IA aprueba o rechaza el pago con Crédito Apro de un pedido.",
            "destinatarios": "Quien hizo el pedido en Shopify; si no hay email, quien pidió la cotización o el contacto principal de la empresa.",
            "boton": "Ver mis pedidos",
            "cuerpo": "Pedido, total, medio de pago y si quedó pagado o pendiente de pago.",
            "nota": "Solo para crédito. Sale una vez por pedido y resultado: un reenvío del mismo estado o un pedido que ya estaba pagado no vuelven a avisar.",
            "captura": _capturar(
                ses.notify_pago_credito_cliente,
                contact_email=EMAIL,
                contact_name=CONTACTO,
                company_name=EMPRESA,
                order_name="#1042",
                aprobado=True,
                total="$1.254.300 CLP",
            ),
            "variante": {
                "etiqueta": "Cuando el crédito no se aprueba",
                "captura": _capturar(
                    ses.notify_pago_credito_cliente,
                    contact_email=EMAIL,
                    contact_name=CONTACTO,
                    company_name=EMPRESA,
                    order_name="#1042",
                    aprobado=False,
                    total="$1.254.300 CLP",
                ),
            },
        },
        {
            "grupo": "Crédito Apro",
            "titulo": "Crédito aprobado (al cliente)",
            "clave": "credito_aprobado",
            "publico": "cliente",
            "disparador": "APRO aprueba el crédito de la empresa: la empresa pasa de pago directo a crédito, por el webhook de estado de crédito o por la sincronización diaria con APRO.",
            "destinatarios": "Todos los contactos con email de la empresa, el principal primero, sin repetir.",
            "boton": "Comprar en Apro Click",
            "cuerpo": "Empresa y los tres pasos para pagar con «Crédito Apro» en el checkout.",
            "nota": "Sale una sola vez por aprobación: un reenvío de «aprobada» a una empresa que ya tenía crédito no vuelve a avisar. Si el crédito se anula y después se aprueba de nuevo, sí se avisa otra vez.",
            "captura": _capturar(
                ses.notify_credito_aprobado,
                contact_email=EMAIL,
                contact_name=CONTACTO,
                company_name=EMPRESA,
            ),
        },
    ]


def _preview(captura: tuple[list[str], str, str], etiqueta: str | None = None) -> str:
    """Iframe aislado con el HTML real del correo."""
    _, _, cuerpo = captura
    srcdoc = html_mod.escape(cuerpo, quote=True)
    encabezado = (
        f'<div class="preview-var">{html_mod.escape(etiqueta)}</div>' if etiqueta else ""
    )
    return f"""
        {encabezado}
        <div class="preview-marco">
          <iframe class="preview" loading="lazy" title="Vista previa del correo"
                  sandbox="allow-same-origin" srcdoc="{srcdoc}"></iframe>
        </div>"""


def _ficha(c: dict) -> str:
    _, asunto, _ = c["captura"]
    destinatarios_reales = ", ".join(c["captura"][0])
    boton = (
        f'<span class="cta-pill">{html_mod.escape(c["boton"])} <span class="arrow">&rarr;</span></span>'
        if c["boton"]
        else '<span class="cta-pill ninguno">Sin botón</span>'
    )
    nota = f'<div class="nota">{html_mod.escape(c["nota"])}</div>' if c["nota"] else ""
    variante = ""
    if c.get("variante"):
        v = c["variante"]
        variante = _preview(v["captura"], v["etiqueta"])
        _, asunto_alt, _ = v["captura"]
        asunto = f'{asunto}<br>{html_mod.escape(asunto_alt)}'
    else:
        asunto = html_mod.escape(asunto)

    return f"""
      <article class="ficha" id="{c['clave']}">
        <div class="ficha-head">
          <h4>{html_mod.escape(c['titulo'])}</h4>
          <span class="chip {c['publico']}">{'Equipo' if c['publico'] == 'equipo' else 'Cliente'}</span>
          <span class="key">{c['clave']}</span>
        </div>
        <div class="ficha-body">
          <dl class="campos">
            <div class="campo"><dt>Se envía cuando</dt><dd>{html_mod.escape(c['disparador'])}</dd></div>
            <div class="campo"><dt>Destinatarios</dt><dd>{html_mod.escape(c['destinatarios'])}</dd></div>
            <div class="campo"><dt>Botón</dt><dd>{boton}</dd></div>
          </dl>
          <div class="asunto">
            <div>
              <div class="eyebrow">Asunto</div>
              <div class="asunto-linea">{asunto}</div>
            </div>
            <div class="contenido">{html_mod.escape(c['cuerpo'])}</div>
            {nota}
          </div>
        </div>
        <details class="vista">
          <summary><span class="tw">Ver el correo</span><span class="hint">como lo recibe {destinatarios_reales}</span></summary>
          <div class="vista-body">{_preview(c['captura'])}{variante}</div>
        </details>
      </article>"""


def _fila(c: dict) -> str:
    _, asunto, _ = c["captura"]
    return f"""
          <tr>
            <td><a class="name" href="#{c['clave']}">{html_mod.escape(c['titulo'])}</a><span class="key">{c['clave']}</span></td>
            <td>{html_mod.escape(c['disparador'].split('.')[0])}</td>
            <td><span class="chip {c['publico']}">{'Equipo' if c['publico'] == 'equipo' else 'Cliente'}</span></td>
            <td class="mono asunto-celda">{html_mod.escape(asunto)}</td>
          </tr>"""


def construir() -> str:
    datos = correos()
    css = ESTILOS.read_text()
    hoy = date.today().strftime("%d de %B de %Y")
    meses = {
        "January": "enero", "February": "febrero", "March": "marzo", "April": "abril",
        "May": "mayo", "June": "junio", "July": "julio", "August": "agosto",
        "September": "septiembre", "October": "octubre", "November": "noviembre",
        "December": "diciembre",
    }
    for en, es in meses.items():
        hoy = hoy.replace(en, es)

    filas = "".join(_fila(c) for c in datos)
    grupos = ""
    for nombre in ("Alta de empresa", "Cotizaciones y pagos", "Crédito Apro"):
        del_grupo = [c for c in datos if c["grupo"] == nombre]
        fichas = "".join(_ficha(c) for c in del_grupo)
        grupos += f"""
    <div class="grupo-titulo">
      <h3>{nombre}</h3>
      <span class="cuenta">{len(del_grupo)} correos</span>
    </div>
    <div class="fichas">{fichas}
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Notificaciones Apro Click</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
  :root {{ color-scheme: light dark; }}
  img {{ max-width: 100%; }}
{css}
</style>
</head>
<body>
<div class="wrap">

  <header class="doc">
    <div class="eyebrow">Apro Click · Referencia interna</div>
    <h1>Correos que envía la plataforma</h1>
    <p class="lede">
      Las once notificaciones automáticas de Apro Click: qué las dispara, a quién llegan
      y cómo se ven. Ningún correo se envía fuera de esta lista.
    </p>
    <div class="meta-row">
      <span>Remitente <b>Apro Click &lt;no-reply@aproclick.cl&gt;</b></span>
      <span>Envío <b>AWS SES · us-east-1</b></span>
      <span>Generado <b>{hoy}</b></span>
    </div>
  </header>

  <section>
    <h2>Cómo sale un correo</h2>
    <div class="pipeline">
      <div class="step">
        <span class="n">PASO 1</span>
        <span class="t">Ocurre el hecho</span>
        <span class="d">Alguien se registra, pide una cotización o APRO IA confirma un pago.</span>
      </div>
      <div class="step">
        <span class="n">PASO 2</span>
        <span class="t">Se encola</span>
        <span class="d">El servicio deja el aviso en una cola y sigue su trabajo.</span>
      </div>
      <div class="step">
        <span class="n">PASO 3</span>
        <span class="t">Se arma el correo</span>
        <span class="d">Un proceso aparte lo toma y arma el HTML con el logo y el botón que corresponde.</span>
      </div>
      <div class="step">
        <span class="n">PASO 4</span>
        <span class="t">Se entrega</span>
        <span class="d">Sale por SES. Si falla, se reintenta solo; el hecho que lo originó nunca se revierte.</span>
      </div>
    </div>
    <div class="nota">
      <b>Por qué importa:</b> el envío está desacoplado a propósito. Si el correo falla, la empresa igual
      queda aprobada y la cotización igual queda creada — el correo se reintenta por su cuenta.
    </div>
  </section>

  <section>
    <h2>Las once notificaciones</h2>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>Correo</th>
            <th>Se envía cuando</th>
            <th>Destinatario</th>
            <th>Asunto</th>
          </tr>
        </thead>
        <tbody>{filas}
        </tbody>
      </table>
    </div>
    <div class="leyenda">
      <span><span class="chip cliente">Cliente</span> va al contacto de la empresa</span>
      <span><span class="chip equipo">Equipo</span> va a todos los SUPERADMIN y ADMIN activos</span>
    </div>
  </section>

  <section>
    <h2>Ficha de cada correo</h2>
    <p class="ayuda">Cada ficha trae el correo real adentro: abrí <b>Ver el correo</b> para verlo tal como llega a la bandeja.</p>
{grupos}
  </section>

  <section>
    <h2>Lo que conviene tener presente</h2>
    <div class="fichas">
      <article class="ficha">
        <div class="ficha-body unica">
          <dl class="campos">
            <div class="campo">
              <dt>Quién recibe los correos internos</dt>
              <dd>
                No hay una lista de distribución que se pueda editar: los destinatarios se calculan en cada envío
                a partir de los usuarios del panel. Para que alguien deje de recibirlos hay que cambiar su rol
                o desactivar su cuenta; para que empiece, darle rol ADMIN o SUPERADMIN —
                o asignarlo como vendedor de la empresa, que en los correos de cotización también recibe copia.
              </dd>
            </div>
            <div class="campo">
              <dt>Todos los correos comparten el mismo diseño</dt>
              <dd>Logo arriba, contenido, botón de acción y pie. Cambiar la plantilla los cambia todos a la vez.</dd>
            </div>
            <div class="campo">
              <dt>Un correo que no llega no rompe nada</dt>
              <dd>
                Si el envío falla, el mensaje se reintenta. La aprobación, la cotización o el pago ya quedaron
                registrados: el correo es aviso, no el hecho.
              </dd>
            </div>
            <div class="campo">
              <dt>Los clientes pueden responder</dt>
              <dd>
                Salen desde <span class="mono">no-reply@aproclick.cl</span>, pero dos de ellos
                (solicitud rechazada y acuse de cotización) invitan expresamente a responder.
                Conviene tener claro quién revisa esa casilla.
              </dd>
            </div>
            <div class="campo">
              <dt>Los datos de las vistas previas son de ejemplo</dt>
              <dd>
                «{EMPRESA}» y sus productos son inventados para este documento.
                Los textos, el diseño y los asuntos sí son exactamente los que se envían.
              </dd>
            </div>
          </dl>
        </div>
      </article>
    </div>
  </section>

  <footer>
    <span>Apro Click — Distribución B2B</span>
    <span>Documento de referencia interna</span>
    <span>Generado con <span class="mono">backend-v1/scripts/generar_doc_notificaciones.py</span></span>
  </footer>

</div>

<script>
  // Cada vista previa se ajusta al alto real del correo la primera vez que se abre.
  document.querySelectorAll("details.vista").forEach(function (d) {{
    d.addEventListener("toggle", function () {{
      if (!d.open) return;
      d.querySelectorAll("iframe.preview").forEach(function (f) {{
        var ajustar = function () {{
          try {{
            var alto = f.contentDocument.body.scrollHeight;
            if (alto > 0) f.style.height = alto + 32 + "px";
          }} catch (e) {{ /* sin acceso: queda el alto por defecto */ }}
        }};
        if (f.contentDocument && f.contentDocument.readyState === "complete") ajustar();
        f.addEventListener("load", ajustar);
        setTimeout(ajustar, 120);
      }});
    }});
  }});
</script>
</body>
</html>
"""


def main() -> int:
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(construir(), encoding="utf-8")
    kb = SALIDA.stat().st_size / 1024
    # Contado, no fijo: al agregar una notificación el número se actualiza solo.
    notificaciones = correos()
    variantes = sum(1 for n in notificaciones if n.get("variante"))
    print(
        f"Generado {SALIDA} ({kb:.0f} KB) con {len(notificaciones)} correos "
        f"y {variantes} variantes."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
