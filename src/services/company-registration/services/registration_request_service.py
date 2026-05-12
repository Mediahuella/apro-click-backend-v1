"""Solicitudes de registro de empresa: bandeja, alta pública y aprobación con Shopify."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
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
from database.models.registration_request import (  # noqa: E402
    CompanyRegistrationRequest,
)
from database.models.user_company import UserCompany  # noqa: E402
from utils.rut import format_rut_stored  # noqa: E402
from utils.shipping_payload import (  # noqa: E402
    canonical_shipping_payload,
    merge_body_shipping_into_payload,
    shipping_for_shopify_b2b,
)
from utils.shopify_customers import (  # noqa: E402
    ShopifyAPIError,
    ensure_shopify_b2b_company,
    format_shopify_api_error_detail,
    resolve_shop_and_token,
)

VALID_COMPANY_TYPES = {"SMALL", "MEDIUM", "BIG"}
VALID_PAYMENT_TYPES = {"DIRECT", "CREDIT"}

# API (docs) <-> base de datos
_STATUS_DB_TO_API: dict[str, str] = {
    "PENDING": "pending_review",
    "APPROVED": "approved",
    "REJECTED": "rejected",
    "NEEDS_INFO": "needs_info",
}
_STATUS_API_TO_DB: dict[str, str] = {v: k for k, v in _STATUS_DB_TO_API.items()}
_STATUS_API_TO_DB.update(
    {
        "PENDING": "PENDING",
        "APPROVED": "APPROVED",
        "REJECTED": "REJECTED",
        "NEEDS_INFO": "NEEDS_INFO",
    }
)


def _split_contact_name(full: str) -> tuple[str | None, str | None]:
    full = (full or "").strip()
    if not full:
        return None, None
    parts = full.split(None, 1)
    if len(parts) == 1:
        return parts[0], None
    return parts[0], parts[1]


def _normalize_status_filter(raw: str | None) -> str | None:
    if raw is None:
        return None
    key = raw.strip()
    if not key:
        return None
    if key in _STATUS_API_TO_DB:
        return _STATUS_API_TO_DB[key]
    raise ValueError(
        f"status inválido: use {sorted(_STATUS_API_TO_DB.keys())}"
    )


def _serialize_request(req: CompanyRegistrationRequest) -> dict[str, Any]:
    payload = req.payload if isinstance(req.payload, dict) else {}
    api_status = _STATUS_DB_TO_API.get(req.status, req.status.lower())
    out: dict[str, Any] = {
        "id": str(req.id),
        "status": api_status,
        "created_at": req.to_dict().get("created_at"),
        "updated_at": req.to_dict().get("updated_at"),
        "notes": req.notes,
        "resolved_company_id": str(req.resolved_company_id)
        if req.resolved_company_id
        else None,
        "resolved_by_user_id": str(req.resolved_by_user_id)
        if req.resolved_by_user_id
        else None,
        "submitted_email": req.submitted_email,
        "company_name": payload.get("company_name"),
        "rut": payload.get("rut"),
        "contact_name": payload.get("contact_name"),
        "contact_email": payload.get("contact_email"),
        "contact_phone": payload.get("contact_phone"),
        "company_type": payload.get("company_type"),
        "payment_type": payload.get("payment_type"),
        "source": payload.get("source"),
        "shop_domain": payload.get("shop_domain"),
        "shipping_address1": payload.get("shipping_address1"),
        "shipping_address2": payload.get("shipping_address2"),
        "shipping_city": payload.get("shipping_city"),
        "shipping_zone_code": payload.get("shipping_zone_code"),
        "shipping_zip": payload.get("shipping_zip"),
        "shipping_country_code": payload.get("shipping_country_code"),
        "shipping_first_name": payload.get("shipping_first_name"),
        "shipping_last_name": payload.get("shipping_last_name"),
        "giro": payload.get("giro"),
        "direccion": payload.get("direccion"),
    }
    return out


class RegistrationRequestService:
    def create_public_request(self, body: dict[str, Any]) -> dict[str, Any]:
        company_name = (body.get("company_name") or "").strip()
        rut_raw = body.get("rut")
        contact_name = (body.get("contact_name") or "").strip()
        contact_email = (body.get("contact_email") or "").strip().lower()
        contact_phone = (body.get("contact_phone") or "").strip() or None
        company_type = (body.get("company_type") or "SMALL").strip().upper()
        payment_type = (body.get("payment_type") or "DIRECT").strip().upper()
        notes = body.get("notes")
        shop_domain = body.get("shop_domain")
        source = (body.get("source") or "shopify_theme").strip()

        if not company_name:
            raise ValueError("'company_name' es obligatorio")
        if not rut_raw:
            raise ValueError("'rut' es obligatorio")
        if not contact_name:
            raise ValueError("'contact_name' es obligatorio")
        if not contact_email or "@" not in contact_email:
            raise ValueError("'contact_email' es obligatorio y debe ser un email válido")
        if company_type not in VALID_COMPANY_TYPES:
            raise ValueError(f"company_type inválido: {sorted(VALID_COMPANY_TYPES)}")
        if payment_type not in VALID_PAYMENT_TYPES:
            raise ValueError(f"payment_type inválido: {sorted(VALID_PAYMENT_TYPES)}")

        rut_stored = format_rut_stored(str(rut_raw))

        payload: dict[str, Any] = {
            "company_name": company_name,
            "rut": rut_stored,
            "contact_name": contact_name,
            "contact_email": contact_email,
            "contact_phone": contact_phone,
            "company_type": company_type,
            "payment_type": payment_type,
            "source": source,
        }
        if isinstance(notes, str) and notes.strip():
            payload["notes"] = notes.strip()
        if isinstance(shop_domain, str) and shop_domain.strip():
            payload["shop_domain"] = shop_domain.strip()

        giro = body.get("giro")
        if isinstance(giro, str) and giro.strip():
            payload["giro"] = giro.strip()
        direccion = body.get("direccion")
        if isinstance(direccion, str) and direccion.strip():
            payload["direccion"] = direccion.strip()

        payload = merge_body_shipping_into_payload(payload, body)

        with get_session() as session:
            self._assert_no_pending_rut(session, rut_stored)
            row = CompanyRegistrationRequest(
                status="PENDING",
                payload=payload,
                submitted_email=contact_email,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _serialize_request(row)

    @staticmethod
    def _assert_no_pending_rut(session: Session, rut_stored: str) -> None:
        q = select(CompanyRegistrationRequest.id).where(
            CompanyRegistrationRequest.status == "PENDING",
            CompanyRegistrationRequest.payload["rut"].astext == rut_stored,
        )
        if session.scalar(q):
            raise ValueError("Ya existe una solicitud pendiente para este RUT")

    def list_requests(
        self,
        *,
        status: str | None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        db_status = _normalize_status_filter(status) if status else None
        with get_session() as session:
            stmt = select(CompanyRegistrationRequest).order_by(
                CompanyRegistrationRequest.created_at.desc()
            )
            if db_status:
                stmt = stmt.where(CompanyRegistrationRequest.status == db_status)
            stmt = stmt.limit(limit).offset(offset)
            rows = list(session.scalars(stmt))
        return {"requests": [_serialize_request(r) for r in rows]}

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        self._validate_uuid(request_id)
        with get_session() as session:
            row = session.get(CompanyRegistrationRequest, uuid.UUID(request_id))
            return _serialize_request(row) if row else None

    def approve_request(
        self,
        request_id: str,
        *,
        approver_user_id: str,
        sales_user_id: str | None = None,
        company_type: str | None = None,
    ) -> dict[str, Any]:
        self._validate_uuid(request_id)
        self._validate_uuid(approver_user_id)
        uid = uuid.UUID(approver_user_id)
        sales_uid: uuid.UUID | None = None
        if sales_user_id:
            self._validate_uuid(sales_user_id)
            sales_uid = uuid.UUID(sales_user_id)

        with get_session() as session:
            req = session.get(CompanyRegistrationRequest, uuid.UUID(request_id))
            if not req:
                raise LookupError(f"Solicitud '{request_id}' no encontrada")
            if req.status != "PENDING":
                raise ValueError(
                    "Solo se pueden aprobar solicitudes en estado pendiente"
                )
            payload = req.payload if isinstance(req.payload, dict) else {}
            company_name = (payload.get("company_name") or "").strip()
            company_type = (
                company_type.strip().upper()
                if company_type
                else (payload.get("company_type") or "SMALL").strip().upper()
            )
            payment_type = (payload.get("payment_type") or "DIRECT").strip().upper()
            contact_email = (payload.get("contact_email") or "").strip()
            contact_name = (payload.get("contact_name") or "").strip()
            contact_phone = (payload.get("contact_phone") or "").strip() or None
            shop_domain = payload.get("shop_domain")

            if company_type not in VALID_COMPANY_TYPES:
                raise ValueError("company_type en solicitud no válido")
            if payment_type not in VALID_PAYMENT_TYPES:
                raise ValueError("payment_type en solicitud no válido")
            if not company_name:
                raise ValueError("payload sin company_name")

            first_name, last_name = _split_contact_name(contact_name)
            shipping = shipping_for_shopify_b2b(
                payload, first_name=first_name, last_name=last_name
            )

        # shop + access_token desde tabla shopify_app_installations (no env)
        try:
            shop, token = resolve_shop_and_token(
                shop_domain.strip() if isinstance(shop_domain, str) else None
            )
        except LookupError as e:
            raise ValueError(str(e)) from e

        try:
            shopify_b2b = ensure_shopify_b2b_company(
                shop,
                token,
                external_id=str(req.id),
                company_name=company_name,
                contact_email=contact_email,
                first_name=first_name,
                last_name=last_name,
                contact_phone=contact_phone,
                shipping=shipping,
            )
            shopify_customer_id = shopify_b2b["shopify_customer_numeric_id"]
            shopify_company_id = shopify_b2b["shopify_company_numeric_id"]
        except ShopifyAPIError as e:
            detail = format_shopify_api_error_detail(e)
            raise ValueError(
                "No se pudo completar la empresa B2B en Shopify. "
                f"Detalle: {detail}. "
                "Si el mensaje no aclara el fallo: tienda Shopify Plus (B2B), app con scopes "
                "read/write_companies y read/write_customers, y OAuth del servicio shopify con "
                "token vigente en shopify_app_installations."
            ) from e

        with get_session() as session:
            req2 = session.get(CompanyRegistrationRequest, uuid.UUID(request_id))
            if not req2 or req2.status != "PENDING":
                raise ValueError("La solicitud cambió de estado; reintente")

            merged = canonical_shipping_payload(payload)
            billing_rut = (payload.get("rut") or "").strip() or None
            billing_giro = (payload.get("giro") or "").strip() or None
            billing_direccion = (payload.get("direccion") or "").strip()
            if not billing_direccion:
                a1 = (merged.get("shipping_address1") or "").strip()
                a2 = (merged.get("shipping_address2") or "").strip()
                ciudad = (merged.get("shipping_city") or "").strip()
                parts = [p for p in (a1, a2, ciudad) if p]
                billing_direccion = ", ".join(parts) if parts else ""
            billing_direccion = billing_direccion.strip() or None
            billing_region = (merged.get("shipping_zone_code") or "").strip() or None

            company = Company(
                name=company_name,
                company_type=company_type,
                payment_type=payment_type,
                shopify_company_id=shopify_company_id,
                billing_rut=billing_rut,
                billing_razon_social=company_name,
                billing_giro=billing_giro,
                billing_direccion=billing_direccion,
                billing_region=billing_region,
            )
            session.add(company)
            session.flush()

            client = Client(
                company_id=company.id,
                shopify_customer_id=shopify_customer_id,
                email=contact_email or None,
                name=contact_name or None,
                phone=contact_phone,
            )
            session.add(client)

            req2.status = "APPROVED"
            req2.resolved_company_id = company.id
            req2.resolved_by_user_id = uid

            if sales_uid:
                session.add(UserCompany(user_id=sales_uid, company_id=company.id))

            session.commit()
            session.refresh(req2)
            session.refresh(company)
            out = _serialize_request(req2)
            out["created_company"] = company.to_dict()
            out["created_client"] = client.to_dict()
            out["shopify_b2b"] = shopify_b2b
            return out

    def reject_request(
        self,
        request_id: str,
        *,
        rejector_user_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self._validate_uuid(request_id)
        self._validate_uuid(rejector_user_id)
        uid = uuid.UUID(rejector_user_id)

        with get_session() as session:
            req = session.get(CompanyRegistrationRequest, uuid.UUID(request_id))
            if not req:
                raise LookupError(f"Solicitud '{request_id}' no encontrada")
            if req.status != "PENDING":
                raise ValueError(
                    "Solo se pueden rechazar solicitudes en estado pendiente"
                )
            req.status = "REJECTED"
            req.resolved_by_user_id = uid
            if reason and reason.strip():
                prev = (req.notes or "").strip()
                block = f"Rechazo: {reason.strip()}"
                req.notes = f"{prev}\n{block}".strip() if prev else block
            session.commit()
            session.refresh(req)
            return _serialize_request(req)

    @staticmethod
    def _validate_uuid(value: str) -> None:
        try:
            uuid.UUID(value)
        except ValueError as e:
            raise ValueError(f"id inválido (se esperaba UUID): {value!r}") from e
