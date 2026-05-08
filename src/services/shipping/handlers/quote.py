"""API interna del cotizador — POST /api/v1/shipping/quote."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
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

from services.shipping_quote import (  # noqa: E402
    LocalityNotFoundError,
    RouteNotFoundError,
    WeightOutOfRangeError,
    get_iva_pct_default,
    get_origin_default,
    list_localidades,
    parse_packages,
    quote,
)
from utils.cognito_auth import (  # noqa: E402
    authenticate,
    parse_bearer_authorization,
)

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()


def _headers_dict() -> dict[str, str]:
    raw = app.current_event.headers
    if not raw:
        return {}
    return {str(k): (v if v is not None else "") for k, v in raw.items()}


def _require_user() -> dict[str, Any]:
    token = parse_bearer_authorization(_headers_dict())
    if not token:
        raise UnauthorizedError("Se requiere Authorization: Bearer")
    try:
        return authenticate(token)
    except PermissionError as e:
        raise UnauthorizedError(str(e))


@app.post("/api/v1/shipping/quote")
@tracer.capture_method
def post_quote() -> dict[str, Any]:
    _require_user()
    body = app.current_event.json_body or {}
    if not isinstance(body, dict):
        raise BadRequestError("body inválido")

    destination = body.get("destination_locality") or body.get("destination")
    if not destination or not isinstance(destination, str):
        raise BadRequestError("destination_locality es requerido (string)")

    try:
        packages = parse_packages(body.get("packages"))
    except ValueError as e:
        raise BadRequestError(str(e))

    origin = body.get("origin") or get_origin_default()
    iva = body.get("iva_pct")
    iva_val = float(iva) if iva is not None else get_iva_pct_default()

    try:
        result = quote(
            destination_locality=destination,
            packages=packages,
            origin=str(origin),
            iva_pct=iva_val,
        )
    except LocalityNotFoundError as e:
        raise NotFoundError(str(e))
    except RouteNotFoundError as e:
        raise BadRequestError(str(e))
    except WeightOutOfRangeError as e:
        raise BadRequestError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error calculando cotización")
        raise InternalServerError("Error calculando cotización")

    return {"statusCode": 200, "message": "OK", "data": result.to_dict()}


@app.get("/api/v1/shipping/localidades")
@tracer.capture_method
def get_localidades() -> dict[str, Any]:
    _require_user()
    return {"statusCode": 200, "message": "OK", "data": list_localidades()}


def lambda_handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)
