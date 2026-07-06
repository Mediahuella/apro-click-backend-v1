"""API cotizaciones B2B.

- ``POST /api/v1/quotes`` — público (X-Api-Key): el cliente solicita desde el carrito.
- ``GET /api/v1/quotes`` y ``GET /api/v1/quotes/{id}`` — Cognito (panel) o storefront
  (X-Api-Key + ``shopify_company_id`` query).
- ``GET /api/v1/quotes/{id}/pdf`` — Cognito / storefront / email (token HMAC); 302 al presigned.
- ``PATCH /api/v1/quotes/{id}`` y ``POST .../draft|invoice|complete`` — SALES/SUPERADMIN.
"""
from __future__ import annotations

import sys
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

from services import quote_service  # noqa: E402
from utils.cognito_quote_access import (  # noqa: E402
    NEGOTIATE_ROLES,
    READ_ROLES,
    get_user_by_cognito_access_token,
    parse_bearer_authorization,
)
from utils.quotes_api_key import require_quotes_api_key, verify_pdf_token  # noqa: E402

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
                    quote_id, shopify_company_id=shopify_company_id
                ):
                    raise NotFoundError("Cotización no encontrada")
            else:
                if user.get("role") not in READ_ROLES:
                    raise ForbiddenError("Sin permisos para ver cotizaciones")
                if not quote_service.get_quote(quote_id, user=user):
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


@app.post("/api/v1/quotes/<quote_id>/draft")
@tracer.capture_method
def create_draft(quote_id: str):
    try:
        user = _require_negotiator()
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
        _require_negotiator()
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
        _require_negotiator()
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
        _require_negotiator()
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
        _require_negotiator()
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
