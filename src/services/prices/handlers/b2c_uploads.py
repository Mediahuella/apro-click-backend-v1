"""HTTP API B2C del servicio prices — carga de precios B2C/WEB.

Endpoints (todos con ``Authorization: Bearer <Cognito access_token>``):

| Método | Path | Roles | Acción |
|--------|------|-------|--------|
| POST   | ``/api/v1/prices/b2c/uploads``             | SUPERADMIN, ADMIN | Sube Excel (base64) y encola job |
| GET    | ``/api/v1/prices/b2c/uploads``             | staff | Lista paginada |
| GET    | ``/api/v1/prices/b2c/uploads/{upload_id}`` | staff | Detalle |

Body de upload (igual al B2B)::

    {"filename": "lista-mayo-2026.xlsx", "content_base64": "UEsDBBQ...", "notes": "..."}

El POST devuelve 202; el procesamiento real (export de variantes + update de
precios en la tienda B2C) lo hace el worker SQS. Polling con
``GET /b2c/uploads/{id}``.
"""
from __future__ import annotations

import base64
import binascii
import sys
import uuid as uuid_mod
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

possible_shared_paths = [
    Path("/var/task/shared"),
    service_root / "shared",
]
for path in possible_shared_paths:
    if path.exists() and (path / "database").exists():
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        break

from sqlalchemy import select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.user import User  # noqa: E402

from services.b2c_upload_service import B2cPriceUploadService  # noqa: E402
from utils.cognito_auth import (  # noqa: E402
    authenticate,
    parse_bearer_authorization,
)

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
service = B2cPriceUploadService()

WRITE_ROLES = frozenset({"SUPERADMIN", "ADMIN"})
MAX_DECODED_BYTES = 5 * 1024 * 1024


def _headers_dict() -> dict[str, str]:
    raw = app.current_event.headers
    if not raw:
        return {}
    return {str(k): (v if v is not None else "") for k, v in raw.items()}


def _require_user(*, write: bool = False) -> dict[str, Any]:
    token = parse_bearer_authorization(_headers_dict())
    if not token:
        raise UnauthorizedError("Se requiere Authorization: Bearer")
    try:
        user = authenticate(token)
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    if write and user.get("role") not in WRITE_ROLES:
        raise UnauthorizedError(
            "Sin permisos: solo SUPERADMIN/ADMIN pueden subir listas de precios B2C"
        )
    return user


def _resolve_user_id_for_audit(cognito_sub: str) -> uuid_mod.UUID | None:
    if not cognito_sub:
        return None
    try:
        with get_session() as session:
            row = session.scalars(
                select(User).where(User.cognito_sub == cognito_sub)
            ).first()
            return row.id if row else None
    except Exception:
        logger.exception(
            "No se pudo resolver users.id desde cognito_sub",
            extra={"cognito_sub": cognito_sub},
        )
        return None


def _query_int(
    name: str, default: int, *, minimum: int = 0, maximum: int | None = None
) -> int:
    qs = app.current_event.query_string_parameters or {}
    raw = qs.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise BadRequestError(f"{name} debe ser entero")
    if v < minimum:
        raise BadRequestError(f"{name} no puede ser menor a {minimum}")
    if maximum is not None and v > maximum:
        raise BadRequestError(f"{name} no puede ser mayor a {maximum}")
    return v


def _decode_excel_payload(body: dict[str, Any]) -> bytes:
    raw_b64 = body.get("content_base64") or body.get("contentBase64")
    if not isinstance(raw_b64, str) or not raw_b64.strip():
        raise BadRequestError(
            "content_base64 es requerido (string base64 con el Excel)"
        )
    try:
        decoded = base64.b64decode(raw_b64, validate=False)
    except (binascii.Error, ValueError) as e:
        raise BadRequestError(f"content_base64 inválido: {e}")
    if not decoded:
        raise BadRequestError("El archivo decodificado está vacío")
    if len(decoded) > MAX_DECODED_BYTES:
        raise BadRequestError(
            f"El archivo supera el máximo permitido "
            f"({MAX_DECODED_BYTES // (1024 * 1024)} MB)"
        )
    return decoded


@app.post("/api/v1/prices/b2c/uploads")
@tracer.capture_method
def create_upload() -> dict[str, Any]:
    user = _require_user(write=True)
    body = app.current_event.json_body or {}
    if not isinstance(body, dict):
        raise BadRequestError("body inválido")

    file_bytes = _decode_excel_payload(body)
    filename = body.get("filename")
    if filename is not None and not isinstance(filename, str):
        raise BadRequestError("filename debe ser texto")
    notes = body.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise BadRequestError("notes debe ser texto")

    uploaded_by = _resolve_user_id_for_audit(user.get("sub", ""))

    try:
        data = service.create_upload(
            file_bytes=file_bytes,
            filename=filename,
            notes=notes,
            uploaded_by_user_id=uploaded_by,
        )
    except ValueError as e:
        raise BadRequestError(str(e))
    except RuntimeError as e:
        logger.exception("Error de configuración al crear upload B2C")
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Error creando upload B2C")
        raise InternalServerError("Error creando upload B2C")

    return {
        "statusCode": 202,
        "message": "Upload B2C encolado para procesamiento",
        "data": data,
    }


@app.get("/api/v1/prices/b2c/uploads")
@tracer.capture_method
def list_uploads() -> dict[str, Any]:
    _require_user()
    limit = _query_int("limit", default=50, minimum=1, maximum=200)
    offset = _query_int("offset", default=0, minimum=0)
    try:
        data = service.list_uploads(limit=limit, offset=offset)
    except Exception:
        logger.exception("Error listando uploads B2C")
        raise InternalServerError("Error listando uploads B2C")
    return {"statusCode": 200, "message": "OK", "data": data}


@app.get("/api/v1/prices/b2c/uploads/<upload_id>")
@tracer.capture_method
def get_upload(upload_id: str) -> dict[str, Any]:
    _require_user()
    try:
        row = service.get_upload(upload_id)
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error consultando upload B2C")
        raise InternalServerError("Error consultando upload B2C")
    if not row:
        raise NotFoundError(f"Upload '{upload_id}' no encontrado")
    return {"statusCode": 200, "message": "OK", "data": row}


def lambda_handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)
