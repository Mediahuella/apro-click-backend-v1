"""API cotizaciones B2B.

- ``POST /api/v1/quotes`` — público (X-Api-Key): el cliente solicita desde el carrito.
- ``GET /api/v1/quotes`` y ``GET /api/v1/quotes/{id}`` — Cognito (panel) o storefront
  (X-Api-Key + ``shopify_company_id`` query).
- ``GET /api/v1/quotes/{id}/pdf`` — Cognito / storefront / email (token HMAC); 302 al presigned.
- ``GET /api/v1/quotes/{id}/pay?token=`` — token HMAC propio (``pay_url`` del
  listado/detalle); 302 al ``invoiceUrl`` del Draft Order (el mismo link de pago
  que Shopify manda por correo). Al pagarlo, el checkout genera el pedido.
- ``PATCH /api/v1/quotes/{id}`` y ``POST .../draft|invoice|complete``, ``GET .../shipping-rates``
  — SALES/ADMIN/SUPERADMIN, y además acotado a las empresas visibles del usuario
  (``_ensure_quote_in_scope``): un SALES no puede negociar cotizaciones de una
  empresa que no le corresponde aunque conozca el ``quote_id``.
- ``POST /api/v1/quotes/webhooks/apro-ia`` — webhook del DMS externo APRO IA
  (header ``x-api-key``); sincroniza ``apro_ia_status`` y avisa a SALES en
  aprobación/rechazo. Ver ``docs/Webhook eventos.md``.
- ``POST /api/v1/quotes/webhooks/apro-ia/transferencias`` — webhook de
  transferencias del mismo DMS (misma ``x-api-key``): marca el pedido como
  pagado (creándolo desde el draft si aún no existía) o anula la cotización.
  Ver ``docs/Transferencias APRO IA.md``.
- ``POST /api/v1/quotes/webhooks/apro-ia/creditos`` — mismo payload y mismo
  comportamiento que el anterior para los pagos con **crédito**; solo cambia
  el tag con que queda marcado el pedido en Shopify (``credito``).
"""
from __future__ import annotations

import sys
from html import escape
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    ForbiddenError,
    InternalServerError,
    NotFoundError,
    UnauthorizedError,
)
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

from services import apro_ia_client  # noqa: E402
from services import quote_service  # noqa: E402
from utils.cognito_quote_access import (  # noqa: E402
    NEGOTIATE_ROLES,
    READ_ROLES,
    get_user_by_cognito_access_token,
    parse_bearer_authorization,
)
from utils.quotes_api_key import (  # noqa: E402
    require_apro_ia_webhook_key,
    require_quotes_api_key,
    verify_pay_token,
    verify_pdf_token,
)

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()


def _headers_dict() -> dict[str, str]:
    raw = app.current_event.headers
    if not raw:
        return {}
    return {str(k): (v if v is not None else "") for k, v in raw.items()}


def _params() -> dict[str, str]:
    return app.current_event.query_string_parameters or {}


def _cognito_user_or_none() -> dict | None:
    """Resuelve el usuario Cognito si viene Authorization Bearer; si no, None."""
    token = parse_bearer_authorization(_headers_dict())
    if not token:
        return None
    try:
        return get_user_by_cognito_access_token(token)
    except PermissionError as e:
        raise UnauthorizedError(str(e))


def _require_negotiator() -> dict:
    user = _cognito_user_or_none()
    if not user:
        raise UnauthorizedError("Se requiere Authorization: Bearer con access token")
    if user.get("role") not in NEGOTIATE_ROLES:
        raise ForbiddenError("Sin permisos para negociar cotizaciones")
    return user


def _ensure_quote_in_scope(quote_id: str, user: dict) -> None:
    """Verifica que ``quote_id`` pertenezca a una empresa visible para ``user``.

    ``_require_negotiator`` solo valida el rol (SALES/ADMIN/SUPERADMIN) — sin
    esto, cualquier SALES podría negociar/enviar invoice/completar una
    cotización de una empresa que no le corresponde con solo conocer el
    ``quote_id``. Reusa ``quote_service.get_quote`` (mismo scope que el
    listado): SUPERADMIN/ADMIN ven todo, SALES solo sus empresas asignadas.
    """
    if not quote_service.get_quote(quote_id, user=user, sync_order=False):
        raise NotFoundError("Cotización no encontrada")


# ---------------------------------------------------------------------------
# Público — crear solicitud desde el carrito
# ---------------------------------------------------------------------------
@app.post("/api/v1/quotes")
@tracer.capture_method
def create_quote():
    try:
        require_quotes_api_key(_headers_dict())
        body = app.current_event.json_body
        if not isinstance(body, dict):
            raise BadRequestError("Body inválido")
        shop_domain = str(body.get("shop_domain") or body.get("shopDomain") or "").strip()
        data = quote_service.create_public_request(shop_domain, body)
        return {"statusCode": 201, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (BadRequestError, UnauthorizedError):
        raise
    except Exception:
        logger.exception("Error creando solicitud de cotización")
        raise InternalServerError("Error creando solicitud de cotización")


# ---------------------------------------------------------------------------
# Webhook APRO IA — eventos de cotización (ver docs/Webhook eventos.md)
# ---------------------------------------------------------------------------
def _log_webhook_rechazado(ruta: str, motivo: str, error: Exception) -> None:
    """Deja rastro de un webhook de APRO IA que no llegó a procesarse.

    Sin esto, un 401 por ``x-api-key`` mala o un 400 por payload inválido se
    va en silencio: la Lambda queda con un START/END mudo en CloudWatch y no
    hay forma de saber si el emisor intentó algo. El body **no** se loguea
    completo a propósito (puede traer datos del cliente); solo los campos
    identificatorios del payload de transferencias.
    """
    body = {}
    try:
        raw = app.current_event.json_body
        if isinstance(raw, dict):
            body = raw
    except Exception:  # noqa: BLE001 — body no-JSON: igual queremos el log
        pass
    logger.warning(
        "Webhook APRO IA rechazado",
        extra={
            "ruta": ruta,
            "motivo": motivo,
            "error": str(error),
            "tiene_api_key": bool(_get_header_value("x-api-key")),
            "folio": body.get("folio"),
            "order": body.get("order"),
            "status": body.get("status"),
        },
    )


def _get_header_value(nombre: str) -> str:
    headers = _headers_dict() or {}
    for k, v in headers.items():
        if str(k).lower() == nombre:
            return str(v or "")
    return ""


def _procesar_webhook_apro_ia(ruta: str, handler, env: str):
    """Tronco común de los cuatro webhooks de APRO IA (eventos/transferencias × prod/QA).

    El **ambiente se decide por la ruta**, no por la ``x-api-key``: APRO IA
    configura las URLs ``/qa`` en su DMS de pruebas y las otras en producción.
    Elegirlo por URL evita depender de que ellos puedan emitir una clave
    distinta por ambiente y hace imposible que un evento de QA entre por la
    ruta productiva.
    """
    try:
        require_apro_ia_webhook_key(_headers_dict())
        body = app.current_event.json_body
        if not isinstance(body, dict):
            raise BadRequestError("Body inválido")
        data = handler(body, env=env)
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        _log_webhook_rechazado(ruta, "api_key", e)
        raise UnauthorizedError(str(e))
    except ValueError as e:
        _log_webhook_rechazado(ruta, "payload", e)
        raise BadRequestError(str(e))
    except (BadRequestError, UnauthorizedError):
        raise
    except Exception:
        logger.exception("Error procesando webhook APRO IA", extra={"ruta": ruta})
        raise InternalServerError(f"Error procesando webhook APRO IA ({ruta})")


@app.post("/api/v1/quotes/webhooks/apro-ia/transferencias")
@tracer.capture_method
def apro_ia_transfer_webhook():
    return _procesar_webhook_apro_ia(
        "transferencias",
        quote_service.handle_apro_ia_transfer_webhook,
        apro_ia_client.ENV_PROD,
    )


@app.post("/api/v1/quotes/webhooks/apro-ia/qa/transferencias")
@tracer.capture_method
def apro_ia_transfer_webhook_qa():
    return _procesar_webhook_apro_ia(
        "transferencias-qa",
        quote_service.handle_apro_ia_transfer_webhook,
        apro_ia_client.ENV_QA,
    )


@app.post("/api/v1/quotes/webhooks/apro-ia/creditos")
@tracer.capture_method
def apro_ia_credit_webhook():
    return _procesar_webhook_apro_ia(
        "creditos",
        quote_service.handle_apro_ia_credit_webhook,
        apro_ia_client.ENV_PROD,
    )


@app.post("/api/v1/quotes/webhooks/apro-ia/qa/creditos")
@tracer.capture_method
def apro_ia_credit_webhook_qa():
    return _procesar_webhook_apro_ia(
        "creditos-qa",
        quote_service.handle_apro_ia_credit_webhook,
        apro_ia_client.ENV_QA,
    )


@app.post("/api/v1/quotes/webhooks/apro-ia")
@tracer.capture_method
def apro_ia_webhook():
    return _procesar_webhook_apro_ia(
        "eventos", quote_service.handle_apro_ia_webhook, apro_ia_client.ENV_PROD
    )


@app.post("/api/v1/quotes/webhooks/apro-ia/qa")
@tracer.capture_method
def apro_ia_webhook_qa():
    return _procesar_webhook_apro_ia(
        "eventos-qa", quote_service.handle_apro_ia_webhook, apro_ia_client.ENV_QA
    )


# ---------------------------------------------------------------------------
# Listado / detalle (Cognito o storefront)
# ---------------------------------------------------------------------------
@app.get("/api/v1/quotes")
@tracer.capture_method
def list_quotes():
    try:
        user = _cognito_user_or_none()
        params = _params()
        shopify_company_id = None
        if user is None:
            require_quotes_api_key(_headers_dict())
            shopify_company_id = str(params.get("shopify_company_id") or "").strip()
            if not shopify_company_id:
                raise BadRequestError("shopify_company_id es obligatorio")
        elif user.get("role") not in READ_ROLES:
            raise ForbiddenError("Sin permisos para ver cotizaciones")
        try:
            limit = int(params.get("limit", "50"))
            offset = int(params.get("offset", "0"))
        except ValueError as e:
            raise BadRequestError("limit y offset deben ser enteros") from e
        data = quote_service.list_quotes(
            user=user,
            shopify_company_id=shopify_company_id,
            status=params.get("status"),
            q=params.get("q"),
            limit=limit,
            offset=offset,
        )
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (BadRequestError, UnauthorizedError, ForbiddenError):
        raise
    except Exception:
        logger.exception("Error listando cotizaciones")
        raise InternalServerError("Error listando cotizaciones")


@app.get("/api/v1/quotes/<quote_id>/pdf")
@tracer.capture_method
def download_pdf(quote_id: str):
    try:
        params = _params()
        token = params.get("token")
        if token:
            # Acceso por email (sin login): valida el HMAC del quote_id.
            if not verify_pdf_token(quote_id, token):
                raise UnauthorizedError("Token de descarga inválido")
        else:
            user = _cognito_user_or_none()
            if user is None:
                require_quotes_api_key(_headers_dict())
                shopify_company_id = str(params.get("shopify_company_id") or "").strip()
                if not shopify_company_id:
                    raise BadRequestError("shopify_company_id es obligatorio")
                if not quote_service.get_quote(
                    quote_id, shopify_company_id=shopify_company_id, sync_order=False
                ):
                    raise NotFoundError("Cotización no encontrada")
            else:
                if user.get("role") not in READ_ROLES:
                    raise ForbiddenError("Sin permisos para ver cotizaciones")
                if not quote_service.get_quote(quote_id, user=user, sync_order=False):
                    raise NotFoundError("Cotización no encontrada")
        url = quote_service.get_pdf_presigned_url(quote_id)
        if not url:
            raise NotFoundError("La cotización aún no tiene PDF disponible")
        return Response(status_code=302, content_type="text/plain", body="", headers={"Location": url})
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (BadRequestError, UnauthorizedError, ForbiddenError, NotFoundError):
        raise
    except Exception:
        logger.exception("Error descargando PDF de cotización")
        raise InternalServerError("Error descargando PDF de cotización")


def _pagina_pago(status_code: int, titulo: str, mensaje: str) -> Response:
    """Página mínima para quien abre el link «Ir a pagar» en el navegador."""
    body = (
        "<!DOCTYPE html><html lang=\"es\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(titulo)}</title></head>"
        "<body style=\"font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;"
        "padding:0 1rem;color:#1a1a1a\">"
        f"<h1 style=\"font-size:1.25rem\">{escape(titulo)}</h1>"
        f"<p>{escape(mensaje)}</p></body></html>"
    )
    return Response(status_code=status_code, content_type="text/html; charset=utf-8", body=body)


@app.get("/api/v1/quotes/<quote_id>/pay")
@tracer.capture_method
def pay_quote(quote_id: str):
    """«Ir a pagar»: redirige al link de pago del Draft Order de la cotización."""
    if not verify_pay_token(quote_id, _params().get("token")):
        return _pagina_pago(401, "Link no válido", "El link de pago no es válido.")
    try:
        url = quote_service.get_payment_url(quote_id)
    except LookupError:
        return _pagina_pago(404, "Cotización no encontrada", "No encontramos esta cotización.")
    except quote_service.QuoteNotPayableError as e:
        return _pagina_pago(409, "No se puede pagar", str(e))
    except Exception:
        logger.exception("Error obteniendo el link de pago de la cotización", extra={"quote_id": quote_id})
        return _pagina_pago(
            502, "No se pudo abrir el pago", "Intenta nuevamente en unos minutos o contacta a tu vendedor."
        )
    return Response(status_code=302, content_type="text/plain", body="", headers={"Location": url})


@app.post("/api/v1/quotes/<quote_id>/draft")
@tracer.capture_method
def create_draft(quote_id: str):
    try:
        user = _require_negotiator()
        _ensure_quote_in_scope(quote_id, user)
        seller_email = user.get("email") or ""
        data = quote_service.negotiate_draft(quote_id, seller_email=seller_email)
        return {"statusCode": 200, "message": "OK", "data": data}
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (UnauthorizedError, ForbiddenError, NotFoundError):
        raise
    except Exception:
        logger.exception("Error creando draft order de cotización")
        raise InternalServerError("Error creando draft order de cotización")


@app.post("/api/v1/quotes/<quote_id>/invoice")
@tracer.capture_method
def send_invoice(quote_id: str):
    try:
        user = _require_negotiator()
        _ensure_quote_in_scope(quote_id, user)
        data = quote_service.send_invoice(quote_id)
        return {"statusCode": 200, "message": "OK", "data": data}
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (UnauthorizedError, ForbiddenError, NotFoundError):
        raise
    except Exception:
        logger.exception("Error enviando invoice de cotización")
        raise InternalServerError("Error enviando invoice de cotización")


@app.post("/api/v1/quotes/<quote_id>/complete")
@tracer.capture_method
def complete_quote(quote_id: str):
    try:
        user = _require_negotiator()
        _ensure_quote_in_scope(quote_id, user)
        data = quote_service.complete(quote_id)
        return {"statusCode": 200, "message": "OK", "data": data}
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (UnauthorizedError, ForbiddenError, NotFoundError):
        raise
    except Exception:
        logger.exception("Error completando cotización")
        raise InternalServerError("Error completando cotización")


@app.patch("/api/v1/quotes/<quote_id>")
@tracer.capture_method
def patch_quote(quote_id: str):
    try:
        user = _require_negotiator()
        _ensure_quote_in_scope(quote_id, user)
        body = app.current_event.json_body
        if not isinstance(body, dict):
            body = {}
        data = quote_service.update_quote_lines(quote_id, body)
        return {"statusCode": 200, "message": "OK", "data": data}
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (UnauthorizedError, ForbiddenError, NotFoundError):
        raise
    except Exception:
        logger.exception("Error actualizando cotización")
        raise InternalServerError("Error actualizando cotización")


@app.get("/api/v1/quotes/<quote_id>/shipping-rates")
@tracer.capture_method
def get_shipping_rates(quote_id: str):
    try:
        user = _require_negotiator()
        _ensure_quote_in_scope(quote_id, user)
        auth_header = (app.current_event.headers or {}).get("authorization") or (app.current_event.headers or {}).get("Authorization") or ""
        data = quote_service.get_shipping_rates(quote_id, auth_header)
        return {"statusCode": 200, "message": "OK", "data": data}
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except (UnauthorizedError, ForbiddenError, BadRequestError, NotFoundError):
        raise
    except Exception:
        logger.exception("Error obteniendo tarifas de envío")
        raise InternalServerError("Error obteniendo tarifas de envío")


@app.get("/api/v1/quotes/<quote_id>")
@tracer.capture_method
def get_quote(quote_id: str):
    try:
        user = _cognito_user_or_none()
        shopify_company_id = None
        if user is None:
            require_quotes_api_key(_headers_dict())
            shopify_company_id = str(_params().get("shopify_company_id") or "").strip()
            if not shopify_company_id:
                raise BadRequestError("shopify_company_id es obligatorio")
        elif user.get("role") not in READ_ROLES:
            raise ForbiddenError("Sin permisos para ver cotizaciones")
        data = quote_service.get_quote(
            quote_id, user=user, shopify_company_id=shopify_company_id
        )
        if not data:
            raise NotFoundError("Cotización no encontrada")
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (BadRequestError, UnauthorizedError, ForbiddenError, NotFoundError):
        raise
    except Exception:
        logger.exception("Error obteniendo cotización")
        raise InternalServerError("Error obteniendo cotización")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)
