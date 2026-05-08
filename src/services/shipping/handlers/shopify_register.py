"""API administrativa para registrar Shopify CarrierService.

Endpoints (todos con Cognito Bearer):

| Método | Path | Acción |
|---|---|---|
| GET    | ``/api/v1/shipping/shopify/registration``        | Lista carrier services |
| POST   | ``/api/v1/shipping/shopify/registration``        | Crea uno (carrierServiceCreate) |
| PATCH  | ``/api/v1/shipping/shopify/registration/{id}``   | Actualiza (carrierServiceUpdate) |
| DELETE | ``/api/v1/shipping/shopify/registration/{id}``   | Borra (carrierServiceDelete) |

Scopes Shopify (sumar a ``shopify.app.toml``):

- ``write_shipping`` para create/update/delete
- ``read_shipping`` para listar

Si no están, Shopify devuelve ``ACCESS_DENIED`` y el handler responde 502 con
el detalle.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    HTTPStatus,
    InternalServerError,
    NotFoundError,
    ServiceError,
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

from services.shopify_carrier_service_admin import (  # noqa: E402
    create_carrier_service,
    delete_carrier_service,
    list_carrier_services,
    update_carrier_service,
)
from utils.cognito_auth import (  # noqa: E402
    authenticate,
    parse_bearer_authorization,
)

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()

WRITE_ROLES = frozenset({"SUPERADMIN", "ADMIN"})


def _headers_dict() -> dict[str, str]:
    raw = app.current_event.headers
    if not raw:
        return {}
    return {str(k): (v if v is not None else "") for k, v in raw.items()}


def _require_user(write: bool = False) -> dict[str, Any]:
    token = parse_bearer_authorization(_headers_dict())
    if not token:
        raise UnauthorizedError("Se requiere Authorization: Bearer")
    try:
        user = authenticate(token)
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    if write and user.get("role") not in WRITE_ROLES:
        raise UnauthorizedError("Sin permisos: solo SUPERADMIN/ADMIN")
    return user


@app.get("/api/v1/shipping/shopify/registration")
@tracer.capture_method
def get_registration() -> dict[str, Any]:
    _require_user()
    qs = app.current_event.query_string_parameters or {}
    shop = qs.get("shop_domain") or None
    flt = qs.get("query") or None
    try:
        first = int(qs.get("first") or 25)
    except (TypeError, ValueError):
        raise BadRequestError("first debe ser entero")
    try:
        items = list_carrier_services(shop, query=flt, first=first)
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        raise ServiceError(HTTPStatus.BAD_GATEWAY, f"Shopify: {e}")
    except Exception:
        logger.exception("Error listando carrier services")
        raise InternalServerError("Error listando carrier services")
    return {"statusCode": 200, "message": "OK", "data": items}


@app.post("/api/v1/shipping/shopify/registration")
@tracer.capture_method
def post_registration() -> dict[str, Any]:
    _require_user(write=True)
    body = app.current_event.json_body or {}
    if not isinstance(body, dict):
        raise BadRequestError("body inválido")

    name = body.get("name") or "Apro Click"
    callback_url = body.get("callback_url") or body.get("callbackUrl")
    if not callback_url:
        raise BadRequestError("callback_url es requerido")
    supports_discovery = bool(
        body.get("supports_service_discovery")
        if "supports_service_discovery" in body
        else body.get("supportsServiceDiscovery", True)
    )
    active = bool(body.get("active", True))
    shop = body.get("shop_domain") or None

    try:
        cs = create_carrier_service(
            name=str(name),
            callback_url=str(callback_url),
            supports_service_discovery=supports_discovery,
            active=active,
            shop_domain=shop,
        )
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        raise ServiceError(HTTPStatus.BAD_GATEWAY, f"Shopify: {e}")
    except Exception:
        logger.exception("Error creando carrier service")
        raise InternalServerError("Error creando carrier service")

    return {"statusCode": 200, "message": "OK", "data": cs}


@app.patch("/api/v1/shipping/shopify/registration/<carrier_id>")
@tracer.capture_method
def patch_registration(carrier_id: str) -> dict[str, Any]:
    _require_user(write=True)
    body = app.current_event.json_body or {}
    if not isinstance(body, dict):
        raise BadRequestError("body inválido")

    name = body.get("name")
    callback_url = body.get("callback_url") or body.get("callbackUrl")
    active = body.get("active")
    shop = body.get("shop_domain") or None

    try:
        cs = update_carrier_service(
            carrier_service_id=carrier_id,
            name=name,
            callback_url=callback_url,
            active=active,
            shop_domain=shop,
        )
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        msg = str(e)
        if "Shopify HTTP" in msg or "carrierServiceUpdate" in msg or "Shopify:" in msg:
            raise ServiceError(HTTPStatus.BAD_GATEWAY, f"Shopify: {msg}")
        raise BadRequestError(msg)
    except Exception:
        logger.exception("Error actualizando carrier service")
        raise InternalServerError("Error actualizando carrier service")

    return {"statusCode": 200, "message": "OK", "data": cs}


@app.delete("/api/v1/shipping/shopify/registration/<carrier_id>")
@tracer.capture_method
def delete_registration(carrier_id: str) -> dict[str, Any]:
    _require_user(write=True)
    qs = app.current_event.query_string_parameters or {}
    shop = qs.get("shop_domain") or None
    try:
        deleted = delete_carrier_service(
            carrier_service_id=carrier_id, shop_domain=shop
        )
    except LookupError as e:
        raise NotFoundError(str(e))
    except ValueError as e:
        raise ServiceError(HTTPStatus.BAD_GATEWAY, f"Shopify: {e}")
    except Exception:
        logger.exception("Error borrando carrier service")
        raise InternalServerError("Error borrando carrier service")

    return {"statusCode": 200, "message": "OK", "data": {"deletedId": deleted}}


def lambda_handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)
