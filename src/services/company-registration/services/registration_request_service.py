"""Solicitudes de registro de empresa: bandeja, alta pública y aprobación con Shopify."""
from __future__ import annotations

import datetime as dt
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from sqlalchemy import case, func, select
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
from database.models.company_address import CompanyAddress  # noqa: E402
from database.models.price_list import ShopifyPriceSegment  # noqa: E402
from database.models.registration_request import (  # noqa: E402
    CompanyRegistrationRequest,
)
from database.models.user import User  # noqa: E402
from database.models.user_company import UserCompany  # noqa: E402
from utils.chile_shopify_zone import zone_code_to_region_name  # noqa: E402
from utils.rut import format_rut_stored  # noqa: E402
from notifications.sqs_publisher import encolar_aprobacion, encolar_nueva_solicitud, encolar_rechazo  # noqa: E402
from utils.shipping_payload import (  # noqa: E402
    canonical_shipping_payload,
    merge_body_shipping_into_payload,
    shipping_for_shopify_b2b,
)
from utils.shopify_customers import (  # noqa: E402
    ShopifyAPIError,
    add_locations_to_catalog,
    ensure_shopify_b2b_company,
    format_shopify_api_error_detail,
    resolve_shop_and_token,
)

_COMPANY_TYPE_TO_SEGMENT: dict[str, str] = {
    "SMALL": "PYME",
    "MEDIUM": "MEDIANA",
    "BIG": "GRAN_EMPRESA",
}

VALID_COMPANY_TYPES = {"SMALL", "MEDIUM", "BIG"}
VALID_PAYMENT_TYPES = {"DIRECT", "CREDIT"}

# RUT de prueba exento del control de duplicados (formato canónico de
# format_rut_stored). Permite reutilizarlo en cualquiera de sus variantes.
RUT_DUPLICADO_EXENTO = "76943201-9"

# Vendedores "internos/dev" — sólo SUPERADMIN los ve en el KPI de referidos.
# Mantener en minúsculas (comparación case-insensitive contra `users.email`).
_REFERRAL_KPI_HIDDEN_EMAILS: frozenset[str] = frozenset({"zobekdev1@gmail.com"})


def _sync_new_company_to_catalog(
    *,
    shop_domain: str,
    access_token: str,
    company_type: str,
    location_gids: list[str],
) -> None:
    """Agrega las CompanyLocations de la empresa recién aprobada al catálogo B2B de su segmento.

    No lanza: los errores se loguean como warnings para no bloquear la aprobación.
    """
    if not location_gids:
        logger.warning(
            "_sync_new_company_to_catalog: no se recibieron location_gids de Shopify "
            "(companyCreate no devolvió locations). Usar rebuild-catalog para enlazarlas.",
            extra={"company_type": company_type},
        )
        return

    segment = _COMPANY_TYPE_TO_SEGMENT.get(company_type)
    if not segment:
        return

    with get_session() as session:
        seg_row = session.scalar(
            select(ShopifyPriceSegment).where(ShopifyPriceSegment.segment == segment)
        )
        catalog_gid = seg_row.catalog_gid if seg_row else None

    if not catalog_gid:
        logger.warning(
            "_sync_new_company_to_catalog: el catálogo del segmento %s no existe aún en BD. "
            "Subir un Excel de precios para crearlo; luego usar rebuild-catalog.",
            segment,
            extra={"company_type": company_type, "location_gids": location_gids},
        )
        return

    try:
        add_locations_to_catalog(shop_domain, access_token, catalog_gid, location_gids)
        logger.info(
            "_sync_new_company_to_catalog: locations agregadas al catálogo %s",
            segment,
            extra={"locations": len(location_gids)},
        )
    except Exception as e:
        logger.warning(
            "_sync_new_company_to_catalog: no se pudo agregar al catálogo %s — %s",
            segment,
            e,
            extra={"location_gids": location_gids},
        )

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


def _marcar_notificado(request_id: uuid.UUID, columna: str) -> None:
    """Setea `approval_notified_at` o `rejection_notified_at` = now() en una sesión nueva.

    Se llama después de encolar el email. Es non-blocking: si falla, se loguea pero
    no se propaga el error porque el evento de negocio (aprobación/rechazo) ya quedó
    persistido en la transacción anterior.
    """
    if columna not in ("approval_notified_at", "rejection_notified_at"):
        raise ValueError(f"columna inválida: {columna!r}")
    try:
        with get_session() as session:
            req = session.get(CompanyRegistrationRequest, request_id)
            if req is None:
                return
            setattr(req, columna, dt.datetime.now(dt.UTC))
            session.commit()
    except Exception:
        logger.exception(
            "no se pudo marcar %s en request %s — el email quedó encolado igual",
            columna,
            request_id,
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
        "referred_by_user_id": str(req.referred_by_user_id)
        if req.referred_by_user_id
        else None,
        "referral_code": payload.get("referral_code"),
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
    def _get_admin_emails(self) -> list[str]:
        """Obtiene emails de todos los SUPERADMIN y ADMIN activos desde la DB."""
        try:
            with get_session() as session:
                users = session.scalars(
                    select(User)
                    .where(User.role.in_(["SUPERADMIN", "ADMIN"]))
                    .where(User.status == "ACTIVE")
                ).all()
                return [u.email for u in users if u.email]
        except Exception as e:
            logger.error("Error obteniendo admin emails", extra={"error": str(e)})
            return []

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
        referral_code_raw = body.get("referral_code")
        referral_code = (
            referral_code_raw.strip()
            if isinstance(referral_code_raw, str) and referral_code_raw.strip()
            else None
        )

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
            self._assert_rut_available(session, rut_stored)
            referred_by_user_id: uuid.UUID | None = None
            if referral_code:
                payload["referral_code"] = referral_code
                # Resuelve a un SALES activo. Si el código no matchea, no falla la solicitud
                # (preferimos perder la atribución antes que rebotar al cliente).
                sales_row = session.scalar(
                    select(User.id).where(
                        User.referral_code == referral_code,
                        User.role == "SALES",
                        User.status == "ACTIVE",
                    )
                )
                if sales_row is not None:
                    referred_by_user_id = sales_row
                else:
                    logger.warning(
                        "create_public_request: referral_code %s no resolvió a un SALES activo",
                        referral_code,
                    )

            row = CompanyRegistrationRequest(
                status="PENDING",
                payload=payload,
                submitted_email=contact_email,
                referred_by_user_id=referred_by_user_id,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            result = _serialize_request(row)

        admin_emails = self._get_admin_emails()
        encolar_nueva_solicitud(
            admin_emails=admin_emails,
            company_name=company_name,
            rut=rut_stored,
            contact_name=contact_name,
            contact_email=contact_email,
            request_id=str(row.id),
        )
        return result

    @staticmethod
    def _assert_rut_available(session: Session, rut_stored: str) -> None:
        # RUT de prueba exento del control de duplicados (todas sus variantes se
        # normalizan a este formato vía format_rut_stored).
        if rut_stored == RUT_DUPLICADO_EXENTO:
            return

        empresa_existente = session.scalar(
            select(Company.id).where(Company.billing_rut == rut_stored)
        )
        if empresa_existente:
            raise ValueError("Ya existe una empresa registrada con este RUT")

        solicitud_existente = session.scalar(
            select(CompanyRegistrationRequest.id).where(
                CompanyRegistrationRequest.status.in_(("PENDING", "APPROVED")),
                CompanyRegistrationRequest.payload["rut"].astext == rut_stored,
            )
        )
        if solicitud_existente:
            raise ValueError("Ya existe una solicitud de registro para este RUT")

    def list_requests(
        self,
        *,
        status: str | None,
        limit: int = 50,
        offset: int = 0,
        resolved_company_id: str | None = None,
    ) -> dict[str, Any]:
        db_status = _normalize_status_filter(status) if status else None
        company_uuid: uuid.UUID | None = None
        if resolved_company_id:
            try:
                company_uuid = uuid.UUID(resolved_company_id)
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"resolved_company_id inválido (esperado UUID): "
                    f"'{resolved_company_id}'"
                ) from e
        with get_session() as session:
            stmt = select(CompanyRegistrationRequest).order_by(
                CompanyRegistrationRequest.created_at.desc()
            )
            if db_status:
                stmt = stmt.where(CompanyRegistrationRequest.status == db_status)
            if company_uuid is not None:
                stmt = stmt.where(
                    CompanyRegistrationRequest.resolved_company_id == company_uuid
                )
            stmt = stmt.limit(limit).offset(offset)
            rows = list(session.scalars(stmt))
        return {"requests": [_serialize_request(r) for r in rows]}

    def get_referral_stats(self, *, caller_role: str | None = None) -> dict[str, Any]:
        """KPI por vendedor (SALES + SUPERADMIN con referral_code).

        Conteos por estado: pending, approved, rejected, total. `companies_count`
        coincide con `approved` (cada solicitud aprobada genera exactamente una
        empresa con `referred_by_user_id` apuntando al vendedor).

        `caller_role`: si NO es SUPERADMIN, oculta usuarios listados en
        `_REFERRAL_KPI_HIDDEN_EMAILS` (cuentas internas/dev) y los descuenta de `totals`.
        """
        approved_case = case(
            (CompanyRegistrationRequest.status == "APPROVED", 1), else_=0
        )
        pending_case = case(
            (CompanyRegistrationRequest.status == "PENDING", 1), else_=0
        )
        rejected_case = case(
            (CompanyRegistrationRequest.status == "REJECTED", 1), else_=0
        )

        with get_session() as session:
            stmt = (
                select(
                    User.id,
                    User.given_name,
                    User.family_name,
                    User.email,
                    User.role,
                    User.referral_code,
                    User.status,
                    func.count(CompanyRegistrationRequest.id).label("total"),
                    func.coalesce(func.sum(approved_case), 0).label("approved"),
                    func.coalesce(func.sum(pending_case), 0).label("pending"),
                    func.coalesce(func.sum(rejected_case), 0).label("rejected"),
                )
                .outerjoin(
                    CompanyRegistrationRequest,
                    CompanyRegistrationRequest.referred_by_user_id == User.id,
                )
                .where(User.role == "SALES")
                .where(User.referral_code.isnot(None))
                .group_by(User.id)
                .order_by(func.count(CompanyRegistrationRequest.id).desc())
            )
            rows = list(session.execute(stmt).all())

        is_superadmin = (caller_role or "").upper() == "SUPERADMIN"
        vendors = []
        for r in rows:
            email = (r.email or "").strip().lower()
            if not is_superadmin and email in _REFERRAL_KPI_HIDDEN_EMAILS:
                continue
            vendors.append(
                {
                    "user_id": str(r.id),
                    "given_name": r.given_name or "",
                    "family_name": r.family_name or "",
                    "email": r.email,
                    "role": r.role,
                    "status": r.status,
                    "referral_code": r.referral_code,
                    "registrations_total": int(r.total or 0),
                    "registrations_approved": int(r.approved or 0),
                    "registrations_pending": int(r.pending or 0),
                    "registrations_rejected": int(r.rejected or 0),
                    "companies_count": int(r.approved or 0),
                }
            )
        totals = {
            "vendors_count": len(vendors),
            "registrations_total": sum(v["registrations_total"] for v in vendors),
            "registrations_approved": sum(v["registrations_approved"] for v in vendors),
            "registrations_pending": sum(v["registrations_pending"] for v in vendors),
            "registrations_rejected": sum(v["registrations_rejected"] for v in vendors),
        }
        return {"vendors": vendors, "totals": totals}

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
        billing_giro: str | None = None,
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

            # billing_giro: override del admin tiene prioridad; si no, lo que envió la empresa.
            _giro_override = (billing_giro or "").strip()
            _giro_payload = (payload.get("giro") or "").strip()
            billing_giro_final = (_giro_override or _giro_payload) or None

            billing_direccion = (payload.get("direccion") or "").strip()
            if not billing_direccion:
                a1 = (merged.get("shipping_address1") or "").strip()
                a2 = (merged.get("shipping_address2") or "").strip()
                ciudad = (merged.get("shipping_city") or "").strip()
                parts = [p for p in (a1, a2, ciudad) if p]
                billing_direccion = ", ".join(parts) if parts else ""
            billing_direccion = billing_direccion.strip() or None

            # billing_region: convertir código de zona al nombre oficial de la región.
            zone_raw = (merged.get("shipping_zone_code") or "").strip()
            billing_region = zone_code_to_region_name(zone_raw) if zone_raw else None
            if not billing_region:
                # Si no se pudo resolver (zona no chilena u otro formato), guardar el valor crudo.
                billing_region = zone_raw or None

            company = Company(
                name=company_name,
                company_type=company_type,
                payment_type=payment_type,
                shopify_company_id=shopify_company_id,
                billing_rut=billing_rut,
                billing_razon_social=company_name,
                billing_giro=billing_giro_final,
                billing_direccion=billing_direccion,
                billing_region=billing_region,
                referred_by_user_id=req2.referred_by_user_id,
            )
            session.add(company)
            session.flush()

            # Si la empresa vino por un link de invitación de un SALES, lo dejamos asignado
            # como vendedor de cartera vía `UserCompany` (mismo mecanismo que el override manual).
            if req2.referred_by_user_id and not sales_uid:
                session.add(
                    UserCompany(
                        user_id=req2.referred_by_user_id, company_id=company.id
                    )
                )

            client = Client(
                company_id=company.id,
                shopify_customer_id=shopify_customer_id,
                email=contact_email or None,
                name=contact_name or None,
                phone=contact_phone,
            )
            session.add(client)

            # Sucursal inicial: espejo en BD de la primera CompanyLocation creada en Shopify.
            initial_location_gids = (
                shopify_b2b.get("shopify_company_location_gids") or []
            )
            if initial_location_gids and shipping.get("address1"):
                primary_address = CompanyAddress(
                    company_id=company.id,
                    label=f"{company_name} — Principal",
                    is_primary=True,
                    shopify_location_id=initial_location_gids[0],
                    address1=str(shipping["address1"]).strip(),
                    address2=(shipping.get("address2") or None),
                    city=str(shipping.get("city") or "").strip() or "—",
                    zone_code=(shipping.get("zone_code") or None),
                    country_code=str(
                        shipping.get("country_code") or "CL"
                    ).strip().upper()[:2],
                    zip=(shipping.get("zip") or None),
                    phone=contact_phone,
                    first_name=(shipping.get("first_name") or first_name),
                    last_name=(shipping.get("last_name") or last_name),
                )
                session.add(primary_address)

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

        # Agregar la CompanyLocation al catálogo B2B del segmento correspondiente.
        _sync_new_company_to_catalog(
            shop_domain=shop,
            access_token=token,
            company_type=company_type,
            location_gids=shopify_b2b.get("shopify_company_location_gids") or [],
        )

        encolar_aprobacion(
            contact_email=payload.get("contact_email", ""),
            contact_name=payload.get("contact_name", ""),
            company_name=company_name,
        )
        _marcar_notificado(uuid.UUID(request_id), "approval_notified_at")
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
            payload = req.payload if isinstance(req.payload, dict) else {}
            result = _serialize_request(req)

        encolar_rechazo(
            contact_email=payload.get("contact_email", ""),
            contact_name=payload.get("contact_name", ""),
            company_name=payload.get("company_name", ""),
            reason=reason,
        )
        _marcar_notificado(uuid.UUID(request_id), "rejection_notified_at")
        return result

    @staticmethod
    def _validate_uuid(value: str) -> None:
        try:
            uuid.UUID(value)
        except ValueError as e:
            raise ValueError(f"id inválido (se esperaba UUID): {value!r}") from e
