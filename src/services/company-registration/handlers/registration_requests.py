"""Solicitudes de alta de empresa: pública (API key) y panel (Cognito)."""
from __future__ import annotations

import sys
from pathlib import Path

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

possible_shared_paths = [
    Path("/var/task/shared"),
    service_root / "shared",
]
for path in possible_shared_paths:
    if path.exists() and (path / "database").exists():
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        break

from services.registration_request_service import RegistrationRequestService  # noqa: E402
from utils.cognito_staff import (  # noqa: E402
    get_user_by_cognito_access_token,
    parse_bearer_authorization,
    require_approver,
)
from utils.public_api_key import require_registration_api_key  # noqa: E402

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
_svc = RegistrationRequestService()


def _headers_dict() -> dict[str, str]:
    raw = app.current_event.headers
    if not raw:
        return {}
    return {str(k): (v if v is not None else "") for k, v in raw.items()}


def _require_staff() -> dict:
    h = _headers_dict()
    token = parse_bearer_authorization(h)
    if not token:
        raise UnauthorizedError("Se requiere Authorization: Bearer con access token")
    try:
        user = get_user_by_cognito_access_token(token)
        require_approver(user)
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    return user


@app.post("/api/v1/company-registration-requests")
@tracer.capture_method
def create_request():
    try:
        require_registration_api_key(_headers_dict())
        body = app.current_event.json_body or {}
        data = _svc.create_public_request(body)
        return {
            "statusCode": 201,
            "message": "Solicitud registrada",
            "data": data,
        }
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error creando solicitud de empresa")
        raise InternalServerError("Error creando solicitud")


@app.get("/api/v1/company-registration-requests")
@tracer.capture_method
def list_requests():
    try:
        _require_staff()
        params = app.current_event.query_string_parameters or {}
        limit = int(params.get("limit", "50"))
        offset = int(params.get("offset", "0"))
        status = params.get("status")
        data = _svc.list_requests(status=status, limit=limit, offset=offset)
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error listando solicitudes")
        raise InternalServerError("Error listando solicitudes")


@app.get("/api/v1/company-registration-requests/<request_id>")
@tracer.capture_method
def get_request(request_id: str):
    try:
        _require_staff()
        row = _svc.get_request(request_id)
        if not row:
            raise NotFoundError("Solicitud no encontrada")
        return {"statusCode": 200, "message": "OK", "data": row}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except Exception:
        logger.exception("Error obteniendo solicitud")
        raise InternalServerError("Error obteniendo solicitud")


@app.post("/api/v1/company-registration-requests/<request_id>/approve")
@tracer.capture_method
def approve_request(request_id: str):
    try:
        user = _require_staff()
        body = app.current_event.json_body or {}
        sales_user_id = body.get("sales_user_id") or None
        company_type = body.get("company_type") or None
        data = _svc.approve_request(
            request_id,
            approver_user_id=user["id"],
            sales_user_id=sales_user_id,
            company_type=company_type,
        )
        return {
            "statusCode": 200,
            "message": "Solicitud aprobada; empresa y cliente creados",
            "data": data,
        }
    except LookupError as e:
        raise NotFoundError(str(e))
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error aprobando solicitud")
        raise InternalServerError("Error aprobando solicitud")


@app.post("/api/v1/company-registration-requests/<request_id>/reject")
@tracer.capture_method
def reject_request(request_id: str):
    try:
        user = _require_staff()
        body = app.current_event.json_body or {}
        reason = body.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise BadRequestError("'reason' debe ser texto")
        data = _svc.reject_request(
            request_id,
            rejector_user_id=user["id"],
            reason=reason,
        )
        return {"statusCode": 200, "message": "Solicitud rechazada", "data": data}
    except LookupError as e:
        raise NotFoundError(str(e))
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except ValueError as e:
        raise BadRequestError(str(e))
    except BadRequestError:
        raise
    except Exception:
        logger.exception("Error rechazando solicitud")
        raise InternalServerError("Error rechazando solicitud")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)
