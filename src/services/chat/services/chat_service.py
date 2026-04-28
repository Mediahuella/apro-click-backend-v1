"""Conversaciones CRM (vendedor ↔ cliente) expuestas al theme y al panel."""
from __future__ import annotations

import os
import re
import sys
import uuid as uuid_mod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

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

from database.engine import get_session  # noqa: E402
from database.models.client import Client  # noqa: E402
from database.models.company import Company  # noqa: E402
from database.models.conversation import Conversation  # noqa: E402
from database.models.message import Message  # noqa: E402
from database.models.shopify import ShopifyAppInstallation  # noqa: E402
from database.models.user import User  # noqa: E402
from utils.ws_broadcaster import broadcast_new_message  # noqa: E402

_GID_SHOPIFY_COMPANY = re.compile(r"^gid://shopify/Company/(\d+)$")

_ALLOWED_CONTENT_TYPES = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/pdf",
})
_S3_PRESIGN_TTL = 3600  # 1 hora para URLs de descarga
_UPLOAD_URL_TTL = 300   # 5 minutos para URLs de subida


def _is_blank(s: str | None) -> bool:
    return s is None or not str(s).strip()


def _normalize_shop_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    if not d:
        raise ValueError("shop inválido")
    if not d.endswith(".myshopify.com"):
        if re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$", d):
            d = f"{d}.myshopify.com"
        else:
            raise ValueError(
                "Dominio de tienda inválido: use tienda.myshopify.com"
            )
    return d


def _normalize_shopify_customer_id(raw: str | None) -> str:
    if not raw or not str(raw).strip():
        raise ValueError(
            "Se requiere shopify_customer_id (cliente logueado en la tienda)"
        )
    s = str(raw).strip()
    if s.startswith("gid://") and "/Customer/" in s:
        return s.split("/Customer/")[-1]
    return s


def _get_attachment_url(key: str | None) -> str | None:
    if not key:
        return None
    bucket = os.environ.get("CHAT_ATTACHMENTS_BUCKET", "")
    if not bucket:
        return None
    s3 = boto3.client("s3")
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=_S3_PRESIGN_TTL,
    )


def _serialize_message(m: Message) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "conversation_id": str(m.conversation_id),
        "sender_type": m.sender_type,
        "message_type": m.message_type,
        "body": m.body,
        "attachment_url": _get_attachment_url(m.attachment_key),
        "created_at": m.to_dict().get("created_at"),
    }


def _conversation_storefront_payload(conv: Conversation) -> dict[str, Any]:
    """Campos alineados con normalizadores del theme (status, is_closed, state 0/1)."""
    st = (conv.status or "OPEN").strip().upper()
    if st not in ("OPEN", "CLOSED"):
        st = "OPEN"
    is_closed = st == "CLOSED"
    d = conv.to_dict()
    return {
        "id": str(conv.id),
        "status": st,
        "is_closed": is_closed,
        "isClosed": is_closed,
        "closed": is_closed,
        "closed_at": d.get("closed_at"),
        "state": 1 if is_closed else 0,
        "last_message_at": d.get("last_message_at"),
        "updated_at": d.get("updated_at"),
    }


def _broadcast(conversation_id: str, message: dict[str, Any]) -> None:
    """Intenta broadcast WebSocket; no interrumpe el flujo si falla."""
    try:
        broadcast_new_message(
            conversation_id=conversation_id,
            message=message,
            region=os.environ.get("REGION", "us-east-2"),
            stage=os.environ.get("STAGE", "dev"),
            ws_api_id=os.environ.get("WS_API_ID", ""),
            connections_table=os.environ.get("WS_CONNECTIONS_TABLE", ""),
        )
    except Exception:
        pass


def _pick_default_seller(session: Session, company_id: uuid_mod.UUID) -> User | None:
    q = (
        select(User)
        .where(
            User.company_id == company_id,
            User.status == "ACTIVE",
            User.role.in_(("ADMIN", "SALES")),
        )
        .order_by(User.created_at.asc())
    )
    return session.scalars(q).first()


def _parse_company_id(raw: str | None) -> uuid_mod.UUID:
    if raw is None or not str(raw).strip():
        raise ValueError("Indique company_id (UUID de la empresa en el CRM, tabla companies)")
    try:
        return uuid_mod.UUID(str(raw).strip())
    except ValueError as e:
        raise ValueError(
            "company_id inválido: debe ser un UUID (companies.id), no el id numérico B2B de Shopify"
        ) from e


def _normalize_shopify_b2b_company_id(raw: str) -> str:
    s = str(raw).strip()
    m = _GID_SHOPIFY_COMPANY.match(s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s
    raise ValueError(
        "shopify_company_id inválido: use el id numérico B2B o gid://shopify/Company/..."
    )


def _company_by_shopify_b2b_id(
    session: Session, numeric_b2b: str
) -> Company | None:
    return session.scalar(
        select(Company).where(Company.shopify_company_id == numeric_b2b)
    )


def resolve_public_company_for_shop(
    session: Session,
    shop_domain: str,
    company_id_raw: str | None,
    shopify_company_id_raw: str | None,
) -> uuid_mod.UUID:
    """Resuelve `companies.id` desde `company_id` (UUID) y/o `shopify_company_id` (B2B Shopify)."""
    has_cid = not _is_blank(company_id_raw)
    has_b2b = not _is_blank(shopify_company_id_raw)
    if not has_cid and not has_b2b:
        raise ValueError(
            "Indique company_id (UUID del CRM) y/o shopify_company_id (Company B2B en Shopify)"
        )

    dom = _normalize_shop_domain(shop_domain)
    inst = session.scalar(
        select(ShopifyAppInstallation).where(
            ShopifyAppInstallation.shop_domain == dom
        )
    )
    if not inst:
        raise LookupError("La tienda no está registrada en shopify_app_installations")

    company_from_b2b: Company | None = None
    if has_b2b:
        norm = _normalize_shopify_b2b_company_id(shopify_company_id_raw or "")
        company_from_b2b = _company_by_shopify_b2b_id(session, norm)
        if company_from_b2b is None:
            raise LookupError(
                "No hay empresa en el CRM con ese shopify_company_id (B2B)"
            )

    uid: uuid_mod.UUID | None = None
    if has_cid:
        uid = _parse_company_id(company_id_raw)
        if session.get(Company, uid) is None:
            raise LookupError("No existe la empresa (company_id) en el CRM")

    if has_cid and has_b2b:
        assert uid is not None and company_from_b2b is not None
        if company_from_b2b.id != uid:
            raise LookupError(
                "company_id y shopify_company_id no corresponden a la misma empresa en el CRM"
            )
        resolved = uid
    elif has_b2b:
        resolved = company_from_b2b.id
    else:
        assert uid is not None
        resolved = uid
        if inst.company_id is None:
            raise LookupError(
                "La tienda no tiene company_id en shopify_app_installations. "
                "Envíe shopify_company_id (sesión B2B) o asigne la empresa en la instalación."
            )

    if inst.company_id is not None and resolved != inst.company_id:
        raise LookupError(
            "La empresa resuelta no coincide con la vinculada a esta tienda (shopify_app_installations)"
        )
    return resolved


def _is_platform_admin_staff(staff: dict[str, Any]) -> bool:
    return staff.get("role") in ("SUPERADMIN", "ADMIN")


def _staff_accessible_company_uuids(staff: dict[str, Any]) -> set[uuid_mod.UUID]:
    s: set[uuid_mod.UUID] = set()
    for x in staff.get("order_company_ids") or ():
        try:
            s.add(uuid_mod.UUID(str(x)))
        except ValueError:
            continue
    return s


def _require_same_company(
    staff: dict[str, Any], company_id: uuid_mod.UUID, query_company: str | None
) -> None:
    """Acceso a la conversación: admin ve todas las empresas; el resto solo `user_companies`."""
    if _is_platform_admin_staff(staff):
        if query_company and str(query_company).strip():
            if uuid_mod.UUID(str(query_company).strip()) != company_id:
                raise PermissionError("company_id no coincide con la conversación")
        return
    accessible = _staff_accessible_company_uuids(staff)
    if not accessible or company_id not in accessible:
        raise PermissionError("Sin acceso a esta conversación")
    if query_company and str(query_company).strip():
        if uuid_mod.UUID(str(query_company).strip()) != company_id:
            raise PermissionError("company_id no coincide con la conversación")


class ChatService:
    def public_post_message(
        self,
        shop_domain: str,
        company_id_raw: str | None,
        shopify_company_id_raw: str | None,
        shopify_customer_id: str,
        body: str | None,
        email: str | None,
        name: str | None,
        message_type: str = "TEXT",
        attachment_key: str | None = None,
    ) -> dict[str, Any]:
        msg_type = (message_type or "TEXT").upper()
        if msg_type not in ("TEXT", "IMAGE", "FILE"):
            raise ValueError("message_type debe ser TEXT, IMAGE o FILE")

        text = (body or "").strip() or None
        if msg_type == "TEXT" and not text:
            raise ValueError("El mensaje no puede estar vacío")
        if msg_type != "TEXT" and not attachment_key:
            raise ValueError("attachment_key requerido para mensajes IMAGE o FILE")

        sid = _normalize_shopify_customer_id(shopify_customer_id)

        with get_session() as session:
            company_id = resolve_public_company_for_shop(
                session,
                shop_domain,
                company_id_raw,
                shopify_company_id_raw,
            )
            company_row = session.get(Company, company_id)
            client = session.scalar(
                select(Client).where(
                    Client.company_id == company_id,
                    Client.shopify_customer_id == sid,
                )
            )
            if not client:
                client = Client(
                    company_id=company_id,
                    shopify_customer_id=sid,
                    email=(email or "").strip() or None,
                    name=(name or "").strip() or None,
                )
                session.add(client)
                session.flush()

            seller = _pick_default_seller(session, company_id)
            conv = session.scalars(
                select(Conversation)
                .where(
                    Conversation.company_id == company_id,
                    Conversation.client_id == client.id,
                    Conversation.status == "OPEN",
                )
                .order_by(
                    desc(Conversation.last_message_at),
                    desc(Conversation.created_at),
                )
            ).first()
            if not conv:
                conv = Conversation(
                    company_id=company_id,
                    seller_user_id=seller.id if seller else None,
                    client_id=client.id,
                    status="OPEN",
                )
                session.add(conv)
                session.flush()

            msg = Message(
                conversation_id=conv.id,
                sender_type="CLIENT",
                sender_user_id=None,
                sender_client_id=client.id,
                message_type=msg_type,
                body=text,
                attachment_key=attachment_key,
            )
            session.add(msg)
            session.flush()
            session.refresh(msg)
            conv.last_message_at = msg.created_at

            serialized = _serialize_message(msg)
            conv_payload = _conversation_storefront_payload(conv)
            out = {
                "conversation_id": str(conv.id),
                "company_id": str(company_id),
                "shopify_company_id": company_row.shopify_company_id
                if company_row
                else None,
                "message": serialized,
                "conversation": conv_payload,
                "status": conv_payload["status"],
                "is_closed": conv_payload["is_closed"],
                "isClosed": conv_payload["isClosed"],
                "closed_at": conv_payload["closed_at"],
                "state": conv_payload["state"],
            }
            session.commit()

        _broadcast(out["conversation_id"], serialized)
        return out

    def public_list_messages(
        self,
        shop_domain: str,
        company_id_raw: str | None,
        shopify_company_id_raw: str | None,
        shopify_customer_id: str,
        conversation_id: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        sid = _normalize_shopify_customer_id(shopify_customer_id)
        cid = uuid_mod.UUID(conversation_id)
        lim = min(max(limit, 1), 100)
        off = max(offset, 0)

        with get_session() as session:
            company_id = resolve_public_company_for_shop(
                session,
                shop_domain,
                company_id_raw,
                shopify_company_id_raw,
            )
            company_row = session.get(Company, company_id)
            client = session.scalar(
                select(Client).where(
                    Client.company_id == company_id,
                    Client.shopify_customer_id == sid,
                )
            )
            if not client:
                raise LookupError("Cliente no encontrado")

            conv = session.get(Conversation, cid)
            if (
                not conv
                or conv.client_id != client.id
                or conv.company_id != company_id
            ):
                raise LookupError("Conversación no encontrada")

            q = (
                select(Message)
                .where(Message.conversation_id == cid)
                .order_by(Message.created_at.asc())
                .offset(off)
                .limit(lim)
            )
            rows = list(session.scalars(q).all())
            conv_payload = _conversation_storefront_payload(conv)
            return {
                "conversation_id": str(cid),
                "company_id": str(company_id),
                "shopify_company_id": company_row.shopify_company_id
                if company_row
                else None,
                "conversation": conv_payload,
                "status": conv_payload["status"],
                "is_closed": conv_payload["is_closed"],
                "isClosed": conv_payload["isClosed"],
                "closed_at": conv_payload["closed_at"],
                "state": conv_payload["state"],
                "messages": [_serialize_message(m) for m in rows],
            }

    def public_list_conversations(
        self,
        shop_domain: str,
        company_id_raw: str | None,
        shopify_company_id_raw: str | None,
        shopify_customer_id: str,
        status: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        """Listado de hilos del comprador en el contexto tienda+empresa (theme extension)."""
        sid = _normalize_shopify_customer_id(shopify_customer_id)
        lim = min(max(limit, 1), 100)
        off = max(offset, 0)
        st = (status or "").strip().upper() or None
        if st and st not in ("OPEN", "CLOSED"):
            raise ValueError("status debe ser OPEN o CLOSED")

        with get_session() as session:
            company_id = resolve_public_company_for_shop(
                session,
                shop_domain,
                company_id_raw,
                shopify_company_id_raw,
            )
            company_row = session.get(Company, company_id)
            client = session.scalar(
                select(Client).where(
                    Client.company_id == company_id,
                    Client.shopify_customer_id == sid,
                )
            )
            if not client:
                return {
                    "company_id": str(company_id),
                    "shopify_company_id": company_row.shopify_company_id
                    if company_row
                    else None,
                    "data": [],
                }

            q = select(Conversation).where(
                Conversation.company_id == company_id,
                Conversation.client_id == client.id,
            )
            if st:
                q = q.where(Conversation.status == st)
            q = q.order_by(
                desc(Conversation.last_message_at),
                desc(Conversation.updated_at),
            ).offset(off).limit(lim)
            rows = list(session.scalars(q).all())
            return {
                "company_id": str(company_id),
                "shopify_company_id": company_row.shopify_company_id
                if company_row
                else None,
                "data": [_conversation_storefront_payload(c) for c in rows],
            }

    def staff_list_conversations(
        self,
        staff: dict[str, Any],
        company_id_query: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        lim = min(max(limit, 1), 100)
        off = max(offset, 0)
        is_global = _is_platform_admin_staff(staff)
        q_raw = (str(company_id_query).strip() if company_id_query else "")

        st = (status or "").strip().upper() or None
        if st and st not in ("OPEN", "CLOSED"):
            raise ValueError("status debe ser OPEN o CLOSED")

        with get_session() as session:
            q = select(Conversation)
            if is_global:
                if q_raw:
                    q = q.where(Conversation.company_id == uuid_mod.UUID(q_raw))
            else:
                accessible = _staff_accessible_company_uuids(staff)
                if not accessible:
                    raise PermissionError(
                        "Su usuario no tiene empresas asignadas; un administrador "
                        "debe asociarle empresas (pedidos y chat) en el panel."
                    )
                if q_raw:
                    cf = uuid_mod.UUID(q_raw)
                    if cf not in accessible:
                        raise PermissionError("Sin acceso a esta empresa")
                    q = q.where(Conversation.company_id == cf)
                else:
                    q = q.where(Conversation.company_id.in_(accessible))
            if st:
                q = q.where(Conversation.status == st)
            q = q.order_by(
                desc(Conversation.last_message_at),
                desc(Conversation.updated_at),
            ).offset(off).limit(lim)
            rows = session.scalars(q).all()
            return {
                "data": [
                    {
                        "id": str(c.id),
                        "company_id": str(c.company_id),
                        "seller_user_id": str(c.seller_user_id)
                        if c.seller_user_id
                        else None,
                        "client_id": str(c.client_id),
                        "status": c.status,
                        "closed_at": c.to_dict().get("closed_at"),
                        "last_message_at": c.to_dict().get("last_message_at"),
                        "updated_at": c.to_dict().get("updated_at"),
                    }
                    for c in rows
                ]
            }

    def staff_list_messages(
        self,
        staff: dict[str, Any],
        conversation_id: str,
        company_id_query: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        cid = uuid_mod.UUID(conversation_id)
        lim = min(max(limit, 1), 100)
        off = max(offset, 0)

        with get_session() as session:
            conv = session.get(Conversation, cid)
            if not conv:
                raise LookupError("Conversación no encontrada")
            _require_same_company(staff, conv.company_id, company_id_query)

            q = (
                select(Message)
                .where(Message.conversation_id == cid)
                .order_by(Message.created_at.asc())
                .offset(off)
                .limit(lim)
            )
            rows = list(session.scalars(q).all())
            return {
                "conversation_id": str(cid),
                "messages": [_serialize_message(m) for m in rows],
            }

    def staff_post_message(
        self,
        staff: dict[str, Any],
        conversation_id: str,
        body: str | None,
        company_id_query: str | None,
        message_type: str = "TEXT",
        attachment_key: str | None = None,
    ) -> dict[str, Any]:
        msg_type = (message_type or "TEXT").upper()
        if msg_type not in ("TEXT", "IMAGE", "FILE"):
            raise ValueError("message_type debe ser TEXT, IMAGE o FILE")

        text = (body or "").strip() or None
        if msg_type == "TEXT" and not text:
            raise ValueError("El mensaje no puede estar vacío")
        if msg_type != "TEXT" and not attachment_key:
            raise ValueError("attachment_key requerido para mensajes IMAGE o FILE")

        uid = uuid_mod.UUID(str(staff["id"]))
        cid = uuid_mod.UUID(conversation_id)

        with get_session() as session:
            conv = session.get(Conversation, cid)
            if not conv:
                raise LookupError("Conversación no encontrada")
            _require_same_company(staff, conv.company_id, company_id_query)

            if conv.status == "CLOSED":
                raise PermissionError(
                    "La conversación está cerrada; reábrala (PATCH status=OPEN) para enviar mensajes"
                )

            user = session.get(User, uid)
            if not user:
                raise PermissionError("No puede enviar en esta conversación")

            if conv.seller_user_id is None:
                conv.seller_user_id = uid

            msg = Message(
                conversation_id=conv.id,
                sender_type="USER",
                sender_user_id=uid,
                sender_client_id=None,
                message_type=msg_type,
                body=text,
                attachment_key=attachment_key,
            )
            session.add(msg)
            session.flush()
            session.refresh(msg)
            conv.last_message_at = msg.created_at
            serialized = _serialize_message(msg)
            session.commit()

        _broadcast(str(cid), serialized)
        return serialized

    def staff_update_conversation(
        self,
        staff: dict[str, Any],
        conversation_id: str,
        status: str,
        company_id_query: str | None,
    ) -> dict[str, Any]:
        st = (status or "").strip().upper()
        if st not in ("OPEN", "CLOSED"):
            raise ValueError("status debe ser OPEN o CLOSED")

        cid = uuid_mod.UUID(conversation_id)
        with get_session() as session:
            conv = session.get(Conversation, cid)
            if not conv:
                raise LookupError("Conversación no encontrada")
            _require_same_company(staff, conv.company_id, company_id_query)
            conv.status = st
            if st == "CLOSED":
                conv.closed_at = datetime.now(timezone.utc)
            else:
                conv.closed_at = None
            session.commit()
            session.refresh(conv)
            d = conv.to_dict()
            return {
                "id": str(conv.id),
                "company_id": str(conv.company_id),
                "seller_user_id": str(conv.seller_user_id)
                if conv.seller_user_id
                else None,
                "client_id": str(conv.client_id),
                "status": conv.status,
                "closed_at": d.get("closed_at"),
                "last_message_at": d.get("last_message_at"),
                "updated_at": d.get("updated_at"),
            }

    def create_upload_url(
        self,
        staff: dict[str, Any],
        conversation_id: str,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise ValueError(
                f"Tipo de archivo no permitido: {content_type}. "
                "Use image/jpeg, image/png, image/webp, image/gif, "
                "application/pdf o .xlsx/.xls."
            )

        bucket = os.environ.get("CHAT_ATTACHMENTS_BUCKET", "")
        if not bucket:
            raise RuntimeError("CHAT_ATTACHMENTS_BUCKET no está configurado")

        cid = uuid_mod.UUID(conversation_id)
        file_id = str(uuid_mod.uuid4())

        with get_session() as session:
            conv = session.get(Conversation, cid)
            if not conv:
                raise LookupError("Conversación no encontrada")
            _require_same_company(staff, conv.company_id, None)
            if conv.status == "CLOSED":
                raise PermissionError(
                    "La conversación está cerrada; no se pueden subir adjuntos"
                )
            s3_key = f"{conv.company_id}/{cid}/{file_id}/{filename}"

        s3 = boto3.client("s3")
        presigned_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=_UPLOAD_URL_TTL,
        )
        return {
            "upload_url": presigned_url,
            "attachment_key": s3_key,
            "expires_in": _UPLOAD_URL_TTL,
        }

    def public_create_upload_url(
        self,
        shop_domain: str,
        company_id_raw: str | None,
        shopify_company_id_raw: str | None,
        shopify_customer_id: str,
        conversation_id: str,
        filename: str,
        content_type: str,
    ) -> dict[str, Any]:
        """Genera presigned URL PUT para que el cliente de la tienda suba un adjunto.

        Solo se permiten imágenes (no Excel ni PDF) desde el canal público.
        """
        _PUBLIC_ALLOWED_TYPES = frozenset({
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/gif",
        })
        if content_type not in _PUBLIC_ALLOWED_TYPES:
            raise ValueError(
                f"Tipo de archivo no permitido desde la tienda: {content_type}. "
                "Solo se permiten imágenes: jpeg, png, webp, gif."
            )

        bucket = os.environ.get("CHAT_ATTACHMENTS_BUCKET", "")
        if not bucket:
            raise RuntimeError("CHAT_ATTACHMENTS_BUCKET no está configurado")

        sid = _normalize_shopify_customer_id(shopify_customer_id)
        cid = uuid_mod.UUID(conversation_id)
        file_id = str(uuid_mod.uuid4())

        with get_session() as session:
            company_id = resolve_public_company_for_shop(
                session, shop_domain, company_id_raw, shopify_company_id_raw
            )
            client = session.scalar(
                select(Client).where(
                    Client.company_id == company_id,
                    Client.shopify_customer_id == sid,
                )
            )
            if not client:
                raise LookupError("Cliente no encontrado")

            conv = session.get(Conversation, cid)
            if (
                not conv
                or conv.client_id != client.id
                or conv.company_id != company_id
            ):
                raise LookupError("Conversación no encontrada")
            if conv.status == "CLOSED":
                raise PermissionError(
                    "La conversación está cerrada; no se pueden subir adjuntos"
                )

            s3_key = f"{company_id}/{cid}/{file_id}/{filename}"

        s3 = boto3.client("s3")
        presigned_url = s3.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": s3_key,
                "ContentType": content_type,
            },
            ExpiresIn=_UPLOAD_URL_TTL,
        )
        return {
            "upload_url": presigned_url,
            "attachment_key": s3_key,
            "expires_in": _UPLOAD_URL_TTL,
        }
