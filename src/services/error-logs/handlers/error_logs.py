"""Logs de errores del frontend: POST autenticado (guarda) + GET SUPERADMIN (lista)."""
from __future__ import annotations

import sys
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver
from aws_lambda_powertools.event_handler.exceptions import (
    BadRequestError,
    ForbiddenError,
    InternalServerError,
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

from services.error_log_service import ErrorLogService  # noqa: E402
from utils.cognito_staff import (  # noqa: E402
    get_user_by_cognito_access_token,
    parse_bearer_authorization,
    require_superadmin,
)

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
_svc = ErrorLogService()


def _headers_dict() -> dict[str, str]:
    raw = app.current_event.headers
    if not raw:
        return {}
    return {str(k): (v if v is not None else "") for k, v in raw.items()}


def _require_authenticated_user() -> dict:
    token = parse_bearer_authorization(_headers_dict())
    if not token:
        raise UnauthorizedError("Se requiere Authorization: Bearer con access token")
    try:
        return get_user_by_cognito_access_token(token)
    except PermissionError as e:
        raise UnauthorizedError(str(e))


@app.post("/api/v1/error-logs")
@tracer.capture_method
def create_error_log():
    user = _require_authenticated_user()
    try:
        body = app.current_event.json_body or {}
        data = _svc.create(body, user)
        return {"statusCode": 201, "message": "Log registrado", "data": data}
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error guardando log de error de frontend")
        raise InternalServerError("Error guardando log")


@app.get("/api/v1/error-logs")
@tracer.capture_method
def list_error_logs():
    user = _require_authenticated_user()
    try:
        require_superadmin(user)
    except PermissionError as e:
        raise ForbiddenError(str(e))
    try:
        params = app.current_event.query_string_parameters or {}
        limit = int(params.get("limit", "100"))
        cursor = params.get("cursor")
        data = _svc.list(limit=limit, cursor=cursor)
        return {"statusCode": 200, "message": "OK", "data": data}
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error listando logs de errores")
        raise InternalServerError("Error listando logs")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)
