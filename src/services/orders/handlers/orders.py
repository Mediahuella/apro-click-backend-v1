"""API pedidos — Cognito; listar, ver, intervenir (PENDING)."""
from __future__ import annotations

import sys
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
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

from services.order_service import (  # noqa: E402
    apply_order_updates,
    get_line_item_featured_images_for_order,
    get_order_for_user,
    list_orders_for_user,
)
from utils.cognito_order_access import (  # noqa: E402
    READ_ROLES,
    get_user_by_cognito_access_token,
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


def _require_reader() -> dict:
    h = _headers_dict()
    token = parse_bearer_authorization(h)
    if not token:
        raise UnauthorizedError("Se requiere Authorization: Bearer con access token")
    try:
        user = get_user_by_cognito_access_token(token)
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    if user.get("role") not in READ_ROLES:
        raise UnauthorizedError("Sin permisos para ver pedidos")
    return user


@app.get("/api/v1/orders")
@tracer.capture_method
def list_orders():
    try:
        user = _require_reader()
        params = app.current_event.query_string_parameters or {}
        try:
            limit = int(params.get("limit", "50"))
            offset = int(params.get("offset", "0"))
        except ValueError as e:
            raise BadRequestError("limit y offset deben ser enteros") from e
        status = params.get("status")
        if status and status not in ("PENDING", "CLOSED"):
            raise ValueError("status debe ser PENDING o CLOSED")
        # Alcance: usuario Cognito + user_companies (user["order_company_ids"]).
        # company_id en query es opcional (refinar); no hace falta para filtrar por empresas asignadas.
        company_id = params.get("company_id")
        data = list_orders_for_user(
            user, limit=limit, offset=offset, status=status, company_id=company_id
        )
        return {"statusCode": 200, "message": "OK", "data": data}
    except UnauthorizedError:
        raise
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error listando pedidos")
        raise InternalServerError("Error listando pedidos")


@app.get("/api/v1/orders/<order_id>/line-images")
@tracer.capture_method
def order_line_images(order_id: str):
    try:
        user = _require_reader()
        data = get_line_item_featured_images_for_order(user, order_id)
        if data is None:
            raise NotFoundError("Pedido no encontrado")
        return {"statusCode": 200, "message": "OK", "data": data}
    except UnauthorizedError:
        raise
    except NotFoundError:
        raise
    except Exception:
        logger.exception("Error obteniendo imágenes de líneas")
        raise InternalServerError("Error obteniendo imágenes de líneas")


@app.get("/api/v1/orders/<order_id>")
@tracer.capture_method
def get_order(order_id: str):
    try:
        user = _require_reader()
        data = get_order_for_user(user, order_id)
        if not data:
            raise NotFoundError("Pedido no encontrado")
        return {"statusCode": 200, "message": "OK", "data": data}
    except UnauthorizedError:
        raise
    except NotFoundError:
        raise
    except Exception:
        logger.exception("Error obteniendo pedido")
        raise InternalServerError("Error obteniendo pedido")


@app.patch("/api/v1/orders/<order_id>")
@tracer.capture_method
def patch_order(order_id: str):
    try:
        user = _require_reader()
        body = app.current_event.json_body
        if not isinstance(body, dict):
            body = {}
        data = apply_order_updates(user, order_id, body)
        return {"statusCode": 200, "message": "OK", "data": data}
    except LookupError as e:
        raise NotFoundError(str(e))
    except PermissionError as e:
        raise ForbiddenError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except UnauthorizedError:
        raise
    except Exception:
        logger.exception("Error en intervención")
        raise InternalServerError("Error en intervención")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)
