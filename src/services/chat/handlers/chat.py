"""API HTTP: chat tienda (theme) y panel (Cognito)."""
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

from services.chat_service import ChatService  # noqa: E402
from utils.cognito_staff import (  # noqa: E402
    get_user_by_cognito_access_token,
    parse_bearer_authorization,
    require_chat_staff,
)
from utils.public_api_key import require_chat_storefront_key  # noqa: E402

logger = Logger()
tracer = Tracer()
app = APIGatewayHttpResolver()
_svc = ChatService()


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
        require_chat_staff(user)
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    return user


@app.post("/api/v1/chat/public/messages")
@tracer.capture_method
def public_post():
    try:
        require_chat_storefront_key(_headers_dict())
        body = app.current_event.json_body or {}
        shop = body.get("shop")
        scid = body.get("shopify_customer_id")
        text = body.get("body")
        email = body.get("email")
        name = body.get("name")
        message_type = body.get("message_type", "TEXT")
        attachment_key = body.get("attachment_key")

        if email is not None and not isinstance(email, str):
            raise BadRequestError("'email' debe ser texto")
        if name is not None and not isinstance(name, str):
            raise BadRequestError("'name' debe ser texto")
        if not isinstance(shop, str) or not shop.strip():
            raise BadRequestError("Indique 'shop' (dominio myshopify.com)")

        company_id = body.get("company_id")
        shopify_company_id = body.get("shopify_company_id")
        if company_id is not None and not isinstance(company_id, str):
            raise BadRequestError(
                "'company_id' debe ser texto (UUID de companies en el CRM)"
            )
        if shopify_company_id is not None and not isinstance(shopify_company_id, str):
            raise BadRequestError(
                "'shopify_company_id' debe ser texto (id B2B o GID de Company en Shopify)"
            )
        c_raw = (str(company_id).strip() if company_id else None) or None
        s_b2b = (
            (str(shopify_company_id).strip() if shopify_company_id else None) or None
        )
        if (not c_raw) and (not s_b2b):
            raise BadRequestError(
                "Indique company_id (UUID) y/o shopify_company_id (B2B / sesión de compañía)"
            )
        data = _svc.public_post_message(
            str(shop).strip(),
            c_raw,
            s_b2b,
            str(scid) if scid is not None else "",
            text,
            email,
            name,
            message_type=message_type,
            attachment_key=attachment_key,
        )
        return {"statusCode": 201, "message": "Mensaje enviado", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (ValueError, TypeError) as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise NotFoundError(str(e))
    except Exception:
        logger.exception("Error en chat público (post)")
        raise InternalServerError("Error al enviar mensaje")


@app.get("/api/v1/chat/public/conversations")
@tracer.capture_method
def public_list_conversations():
    try:
        require_chat_storefront_key(_headers_dict())
        params = app.current_event.query_string_parameters or {}
        shop = params.get("shop")
        scid = params.get("shopify_customer_id")
        if not shop or not str(shop).strip():
            raise BadRequestError("Query: shop")
        if not scid or not str(scid).strip():
            raise BadRequestError("Query: shopify_customer_id")
        q_company = (
            params.get("company_id")
            or params.get("Company_Id")
            or params.get("companyId")
        )
        q_b2b = (
            params.get("shopify_company_id")
            or params.get("shopifyCompanyId")
        )
        c_raw = (str(q_company).strip() if q_company else None) or None
        b_raw = (str(q_b2b).strip() if q_b2b else None) or None
        if (not c_raw) and (not b_raw):
            raise BadRequestError(
                "Query: company_id (UUID) y/o shopify_company_id (B2B)"
            )
        limit = int(params.get("limit", "30"))
        offset = int(params.get("offset", "0"))
        status = params.get("status")
        data = _svc.public_list_conversations(
            str(shop).strip(),
            c_raw,
            b_raw,
            str(scid).strip(),
            status,
            limit,
            offset,
        )
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (ValueError, TypeError) as e:
        raise BadRequestError(str(e))
    except BadRequestError:
        raise
    except Exception:
        logger.exception("Error en chat público (listado conversaciones)")
        raise InternalServerError("Error al listar conversaciones")


@app.get("/api/v1/chat/public/conversations/<conv_id>/messages")
@tracer.capture_method
def public_list(conv_id: str):
    try:
        require_chat_storefront_key(_headers_dict())
        params = app.current_event.query_string_parameters or {}
        shop = params.get("shop")
        scid = params.get("shopify_customer_id")
        if not shop or not str(shop).strip():
            raise BadRequestError("Query: shop")
        if not scid or not str(scid).strip():
            raise BadRequestError("Query: shopify_customer_id")
        q_company = (
            params.get("company_id")
            or params.get("Company_Id")
            or params.get("companyId")
        )
        q_b2b = (
            params.get("shopify_company_id")
            or params.get("shopifyCompanyId")
        )
        c_raw = (str(q_company).strip() if q_company else None) or None
        b_raw = (str(q_b2b).strip() if q_b2b else None) or None
        if (not c_raw) and (not b_raw):
            raise BadRequestError(
                "Query: company_id (UUID) y/o shopify_company_id (B2B)"
            )
        limit = int(params.get("limit", "50"))
        offset = int(params.get("offset", "0"))
        data = _svc.public_list_messages(
            str(shop).strip(),
            c_raw,
            b_raw,
            str(scid).strip(),
            conv_id,
            limit,
            offset,
        )
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (ValueError, TypeError) as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise NotFoundError(str(e))
    except BadRequestError:
        raise
    except Exception:
        logger.exception("Error en chat público (list)")
        raise InternalServerError("Error al listar mensajes")


@app.get("/api/v1/chat/conversations")
@tracer.capture_method
def staff_list_conversations():
    try:
        user = _require_staff()
        params = app.current_event.query_string_parameters or {}
        limit = int(params.get("limit", "30"))
        offset = int(params.get("offset", "0"))
        status = params.get("status")
        company_id = params.get("company_id")
        data = _svc.staff_list_conversations(
            user, company_id, status, limit, offset
        )
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (ValueError, TypeError) as e:
        raise BadRequestError(str(e))
    except Exception:
        logger.exception("Error listando conversaciones")
        raise InternalServerError("Error al listar conversaciones")


@app.get("/api/v1/chat/conversations/<conv_id>/messages")
@tracer.capture_method
def staff_list_messages(conv_id: str):
    try:
        user = _require_staff()
        params = app.current_event.query_string_parameters or {}
        limit = int(params.get("limit", "100"))
        offset = int(params.get("offset", "0"))
        company_id = params.get("company_id")
        data = _svc.staff_list_messages(
            user, conv_id, company_id, limit, offset
        )
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (ValueError, TypeError) as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise NotFoundError(str(e))
    except Exception:
        logger.exception("Error listando mensajes (panel)")
        raise InternalServerError("Error al listar mensajes")


@app.patch("/api/v1/chat/conversations/<conv_id>")
@tracer.capture_method
def staff_patch_conversation(conv_id: str):
    try:
        user = _require_staff()
        params = app.current_event.query_string_parameters or {}
        company_id = params.get("company_id")
        body = app.current_event.json_body or {}
        status = body.get("status")
        if status is None or (isinstance(status, str) and not status.strip()):
            raise BadRequestError("Body JSON: status (OPEN o CLOSED)")
        data = _svc.staff_update_conversation(
            user, conv_id, str(status), company_id
        )
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (ValueError, TypeError) as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise NotFoundError(str(e))
    except Exception:
        logger.exception("Error actualizando conversación (panel)")
        raise InternalServerError("Error al actualizar conversación")


@app.post("/api/v1/chat/conversations/<conv_id>/messages")
@tracer.capture_method
def staff_post(conv_id: str):
    try:
        user = _require_staff()
        params = app.current_event.query_string_parameters or {}
        company_id = params.get("company_id")
        body = app.current_event.json_body or {}
        text = body.get("body")
        message_type = body.get("message_type", "TEXT")
        attachment_key = body.get("attachment_key")
        data = _svc.staff_post_message(
            user,
            conv_id,
            text,
            company_id,
            message_type=message_type,
            attachment_key=attachment_key,
        )
        return {"statusCode": 201, "message": "Mensaje enviado", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (ValueError, TypeError) as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise NotFoundError(str(e))
    except Exception:
        logger.exception("Error enviando mensaje (panel)")
        raise InternalServerError("Error al enviar mensaje")


@app.post("/api/v1/chat/public/conversations/<conv_id>/upload")
@tracer.capture_method
def public_request_upload(conv_id: str):
    """Genera presigned URL PUT para que el cliente de la tienda suba una imagen."""
    try:
        require_chat_storefront_key(_headers_dict())
        body = app.current_event.json_body or {}
        shop = body.get("shop")
        scid = body.get("shopify_customer_id")
        filename = (body.get("filename") or "").strip()
        content_type = (body.get("content_type") or "").strip()

        if not isinstance(shop, str) or not shop.strip():
            raise BadRequestError("Indique 'shop'")
        if not scid:
            raise BadRequestError("shopify_customer_id requerido")
        if not filename:
            raise BadRequestError("filename requerido")
        if not content_type:
            raise BadRequestError("content_type requerido")

        company_id = body.get("company_id")
        shopify_company_id = body.get("shopify_company_id")
        c_raw = (str(company_id).strip() if company_id else None) or None
        s_b2b = (str(shopify_company_id).strip() if shopify_company_id else None) or None
        if not c_raw and not s_b2b:
            raise BadRequestError("Indique company_id y/o shopify_company_id")

        data = _svc.public_create_upload_url(
            str(shop).strip(),
            c_raw,
            s_b2b,
            str(scid),
            conv_id,
            filename,
            content_type,
        )
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (ValueError, TypeError) as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise NotFoundError(str(e))
    except Exception:
        logger.exception("Error generando upload URL (público)")
        raise InternalServerError("Error al generar URL de subida")


@app.post("/api/v1/chat/conversations/<conv_id>/upload")
@tracer.capture_method
def staff_request_upload(conv_id: str):
    """Genera una presigned URL PUT de S3 para subir un adjunto directamente."""
    try:
        user = _require_staff()
        body = app.current_event.json_body or {}
        filename = (body.get("filename") or "").strip()
        content_type = (body.get("content_type") or "").strip()
        if not filename:
            raise BadRequestError("filename requerido")
        if not content_type:
            raise BadRequestError("content_type requerido")
        data = _svc.create_upload_url(user, conv_id, filename, content_type)
        return {"statusCode": 200, "message": "OK", "data": data}
    except PermissionError as e:
        raise UnauthorizedError(str(e))
    except (ValueError, TypeError) as e:
        raise BadRequestError(str(e))
    except LookupError as e:
        raise NotFoundError(str(e))
    except Exception:
        logger.exception("Error generando upload URL")
        raise InternalServerError("Error al generar URL de subida")


def lambda_handler(event: dict, context: LambdaContext):
    return app.resolve(event, context)
