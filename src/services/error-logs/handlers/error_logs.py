"""Logs de errores del frontend: POST autenticado (guarda) + GET SUPERADMIN (lista)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import hmac

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

# Permite registrar eventos de expiración de sesión sin Bearer válido (el propio
# motivo del reporte es que ya no queda un access token utilizable). Restringido
# a un tipo de evento fijo, forzado del lado del servidor — nunca confiar en lo
# que mande el cliente por esta vía.
_SESSION_EVENTS_API_KEY = os.environ.get("ERROR_LOGS_SESSION_EVENTS_API_KEY", "")


def _headers_dict() -> dict[str, str]:
    raw = app.current_event.headers
    if not raw:
        return {}
    return {str(k): (v if v is not None else "") for k, v in raw.items()}


def _matches_session_events_api_key(headers: dict[str, str]) -> bool:
    if not _SESSION_EVENTS_API_KEY:
        return False
    provided = headers.get("x-api-key") or headers.get("X-Api-Key")
    if not provided:
        return False
    return hmac.compare_digest(provided, _SESSION_EVENTS_API_KEY)


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
    headers = _headers_dict()
    is_session_event = _matches_session_events_api_key(headers)
    user = (
        {"id": None, "email": None, "role": "SESSION_EVENT"}
        if is_session_event
        else _require_authenticated_user()
    )
    try:
        body = app.current_event.json_body or {}
        if is_session_event:
            body = dict(body)
            context = dict(body.get("context") or {})
            tags = dict(context.get("tags") or {})
            tags["kind"] = "session_expired"
            context["tags"] = tags
            body["context"] = context
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
