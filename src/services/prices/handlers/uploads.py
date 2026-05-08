"""HTTP API del servicio prices — uploads y consulta de status.

Endpoints (todos con ``Authorization: Bearer <Cognito access_token>``):

| Método | Path | Roles | Acción |
|--------|------|-------|--------|
| POST   | ``/api/v1/prices/uploads``                 | SUPERADMIN, ADMIN | Sube Excel (base64) y encola job |
| GET    | ``/api/v1/prices/uploads``                 | staff | Lista paginada |
| GET    | ``/api/v1/prices/uploads/{upload_id}``     | staff | Detalle |
| POST   | ``/api/v1/prices/uploads/{upload_id}/refresh`` | SUPERADMIN, ADMIN | Refresca status desde Shopify |
| GET    | ``/api/v1/prices/segments``                | staff | IDs de Catalog + PriceList por segmento |

Body de upload::

    {
      "filename": "lista-mayo-2026.xlsx",
      "content_base64": "UEsDBBQ...",
      "notes": "Mayo 2026"
    }

El POST devuelve 202 con el ``upload_id``; el procesamiento real (parseo +
push a Shopify) lo hace el worker SQS y puede tardar minutos. Polling con
``GET /uploads/{id}``.
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

# ============================================================================
# CONFIGURAR PATHS PRIMERO
# ============================================================================
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

# ============================================================================
# IMPORTS
# ============================================================================
from sqlalchemy import select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.user import User  # noqa: E402

from services.upload_orchestrator import (  # noqa: E402
    rebuild_segment_catalog,
    refresh_upload_status,
)
from services.upload_service import PriceUploadService  # noqa: E402
from utils.cognito_auth import (  # noqa: E402
    authenticate,
    parse_bearer_authorization,
)

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
service = PriceUploadService()

WRITE_ROLES = frozenset({"SUPERADMIN", "ADMIN"})

#: Tope blando del Excel decodificado. API Gateway HTTP API permite hasta 6 MB;
#: con base64 se infla ~33%, dejamos 5 MB de margen.
MAX_DECODED_BYTES = 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


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
            "Sin permisos: solo SUPERADMIN/ADMIN pueden subir o actualizar listas de precios"
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


# ---------------------------------------------------------------------------
# Helpers varios
# ---------------------------------------------------------------------------


def _query_int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.post("/api/v1/prices/uploads")
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
        # S3/SQS misconfig.
        logger.exception("Error de configuración al crear upload")
        raise InternalServerError(str(e))
    except Exception:
        logger.exception("Error creando upload")
        raise InternalServerError("Error creando upload")

    return {
        "statusCode": 202,
        "message": "Upload encolado para procesamiento",
        "data": data,
    }


@app.get("/api/v1/prices/uploads")
@tracer.capture_method
def list_uploads() -> dict[str, Any]:
    _require_user()
    limit = _query_int("limit", default=50, minimum=1, maximum=200)
    offset = _query_int("offset", default=0, minimum=0)
    try:
        data = service.list_uploads(limit=limit, offset=offset)
    except Exception:
        logger.exception("Error listando uploads")
        raise InternalServerError("Error listando uploads")
    return {"statusCode": 200, "message": "OK", "data": data}


@app.get("/api/v1/prices/uploads/<upload_id>")
@tracer.capture_method
def get_upload(upload_id: str) -> dict[str, Any]:
    _require_user()
    try:
        row = service.get_upload(upload_id)
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error consultando upload")
        raise InternalServerError("Error consultando upload")
    if not row:
        raise NotFoundError(f"Upload '{upload_id}' no encontrado")
    return {"statusCode": 200, "message": "OK", "data": row}


@app.post("/api/v1/prices/uploads/<upload_id>/refresh")
@tracer.capture_method
def refresh_upload(upload_id: str) -> dict[str, Any]:
    _require_user(write=True)
    try:
        uid = uuid_mod.UUID(upload_id)
    except ValueError:
        raise BadRequestError("upload_id inválido")
    try:
        data = refresh_upload_status(uid)
    except LookupError as e:
        raise NotFoundError(str(e))
    except Exception:
        logger.exception("Error refrescando upload status desde Shopify")
        raise InternalServerError("Error refrescando upload status")
    return {"statusCode": 200, "message": "OK", "data": data}


@app.get("/api/v1/prices/segments")
@tracer.capture_method
def get_segments() -> dict[str, Any]:
    _require_user()
    try:
        data = service.list_segments()
    except Exception:
        logger.exception("Error listando segmentos B2B")
        raise InternalServerError("Error listando segmentos B2B")
    return {"statusCode": 200, "message": "OK", "data": data}


@app.post("/api/v1/prices/segments/<segment>/rebuild-catalog")
@tracer.capture_method
def rebuild_catalog(segment: str) -> dict[str, Any]:
    """Crea (o reusa) el ``Catalog`` Shopify del segmento.

    Útil para vincular un ``PriceList`` huérfano (creado por un upload
    cuando aún no había companies aprobadas) con sus ``CompanyLocation``
    una vez que las empresas existen en Shopify.

    El body es opcional: si tiene ``"force": true`` ignoramos el cache
    local de ``catalog_gid`` y verificamos contra Shopify.
    """
    _require_user(write=True)
    seg_norm = (segment or "").strip().upper()
    try:
        data = rebuild_segment_catalog(seg_norm)
    except ValueError as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception(
            "Error reconstruyendo catalog del segmento",
            extra={"segment": seg_norm},
        )
        raise InternalServerError("Error reconstruyendo catalog del segmento")
    return {"statusCode": 200, "message": "OK", "data": data}


def lambda_handler(event: dict, context: LambdaContext) -> dict:
    return app.resolve(event, context)
