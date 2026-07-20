"""Service de direcciones/sucursales por empresa.

Cada ``CompanyAddress`` en la BD es espejo de un ``CompanyLocation`` en Shopify
B2B. La sincronización es síncrona dentro del handler: si la mutation a Shopify
falla, no se commitea la fila local.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger
from sqlalchemy import select, update

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

from database.engine import get_session
from database.models.company import Company
from database.models.company_address import CompanyAddress
from database.models.price_list import ShopifyPriceSegment

from utils.shopify_catalog_sync import (
    COMPANY_TYPE_TO_SEGMENT,
    _catalog_context_update,
    _resolve_shop_and_token,
)
from utils.shopify_locations import (
    build_address,
    create_company_location,
    delete_company_location,
    update_company_location,
)
from utils.shopify_contacts import (
    assign_contact_to_location,
    get_ordering_only_role_id,
)
from services.contact_service import ContactService

logger = Logger()


class AddressService:
    # ------------------------------------------------------------------
    # Lecturas
    # ------------------------------------------------------------------
    def list_addresses(self, company_id: str) -> dict[str, Any]:
        self._validate_uuid(company_id)
        with get_session() as session:
            stmt = (
                select(CompanyAddress)
                .where(CompanyAddress.company_id == uuid.UUID(company_id))
                .order_by(CompanyAddress.created_at.asc())
            )
            rows = [a.to_dict() for a in session.scalars(stmt)]
        return {"addresses": rows}

    def get_address(self, company_id: str, address_id: str) -> dict[str, Any] | None:
        self._validate_uuid(company_id)
        self._validate_uuid(address_id)
        with get_session() as session:
            row = session.get(CompanyAddress, uuid.UUID(address_id))
            if not row or str(row.company_id) != company_id:
                return None
            return row.to_dict()

    # ------------------------------------------------------------------
    # Mutaciones
    # ------------------------------------------------------------------
    def create_address(
        self, company_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_uuid(company_id)
        data = self._validate_payload(payload, require_address=True)
        contact_email = data.pop("contact_email", None)
        contact_name = data.pop("contact_name", None)

        with get_session() as session:
            company = session.get(Company, uuid.UUID(company_id))
            if not company:
                raise ValueError(f"Company '{company_id}' not found")
            if not company.shopify_company_id:
                raise ValueError(
                    "La empresa no tiene shopify_company_id; no se puede crear "
                    "la sucursal en Shopify."
                )
            company_name = company.name
            shopify_company_id = company.shopify_company_id
            company_type = company.company_type

        shop_domain, access_token = _resolve_shop_and_token()

        location_gid = create_company_location(
            shop_domain,
            access_token,
            shopify_company_id=shopify_company_id,
            name=data["label"],
            address1=data["address1"],
            city=data["city"],
            country_code=data["country_code"],
            address2=data.get("address2"),
            zone_code=data.get("zone_code"),
            zip_code=data.get("zip"),
            phone=data.get("phone"),
            first_name=data.get("first_name"),
            last_name=data.get("last_name"),
        )

        try:
            self._add_to_catalog(
                shop_domain, access_token, company_type, [location_gid]
            )
        except Exception:
            # Rollback en Shopify si no se pudo enlazar al catálogo.
            try:
                delete_company_location(shop_domain, access_token, location_gid)
            except Exception:
                logger.exception(
                    "No se pudo borrar la location tras fallo de catálogo",
                    extra={"location_gid": location_gid},
                )
            raise

        is_primary = bool(data.get("is_primary", False))
        try:
            with get_session() as session:
                if is_primary:
                    session.execute(
                        update(CompanyAddress)
                        .where(CompanyAddress.company_id == uuid.UUID(company_id))
                        .values(is_primary=False)
                    )
                row = CompanyAddress(
                    company_id=uuid.UUID(company_id),
                    label=data["label"],
                    is_primary=is_primary,
                    shopify_location_id=location_gid,
                    address1=data["address1"],
                    address2=data.get("address2"),
                    city=data["city"],
                    zone_code=data.get("zone_code"),
                    country_code=data["country_code"],
                    zip=data.get("zip"),
                    phone=data.get("phone"),
                    first_name=data.get("first_name"),
                    last_name=data.get("last_name"),
                )
                session.add(row)
                session.commit()
                session.refresh(row)
                result = row.to_dict()
        except Exception:
            # No se logró persistir: revertir Shopify para mantener consistencia.
            try:
                delete_company_location(shop_domain, access_token, location_gid)
            except Exception:
                logger.exception(
                    "No se pudo borrar la location tras fallo de persistencia",
                    extra={"location_gid": location_gid},
                )
            raise

        logger.info(
            "CompanyAddress creada",
            extra={
                "id": result["id"],
                "company_id": company_id,
                "shopify_location_id": location_gid,
            },
        )
        # company_name no se usa aquí, solo para logs si hace falta
        _ = company_name

        if contact_email:
            self._link_contact(
                company_id=company_id,
                shopify_company_id=shopify_company_id,
                location_gid=location_gid,
                contact_email=contact_email,
                contact_name=contact_name,
                shop_domain=shop_domain,
                access_token=access_token,
                result=result,
            )

        return result

    def update_address(
        self, company_id: str, address_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_uuid(company_id)
        self._validate_uuid(address_id)
        data = self._validate_payload(payload, require_address=False)
        contact_email = data.pop("contact_email", None)
        contact_name = data.pop("contact_name", None)

        with get_session() as session:
            row = session.get(CompanyAddress, uuid.UUID(address_id))
            if not row or str(row.company_id) != company_id:
                raise ValueError(f"Address '{address_id}' not found")
            shopify_location_id = row.shopify_location_id
            current = row.to_dict()

        # Construir address Shopify si algún campo de dirección cambió.
        address_fields = {
            "address1",
            "address2",
            "city",
            "zone_code",
            "country_code",
            "zip",
            "phone",
            "first_name",
            "last_name",
        }
        address_changed = any(k in data for k in address_fields)
        name_changed = "label" in data
        phone_changed = "phone" in data

        if (
            not address_changed
            and not name_changed
            and not phone_changed
            and not contact_email
        ):
            return current

        merged = {**current, **data}

        shop_domain: str | None = None
        access_token: str | None = None
        if shopify_location_id and (address_changed or name_changed or phone_changed):
            shop_domain, access_token = _resolve_shop_and_token()
            address_input = None
            if address_changed:
                address_input = build_address(
                    address1=merged["address1"],
                    city=merged["city"],
                    country_code=merged["country_code"],
                    address2=merged.get("address2"),
                    zone_code=merged.get("zone_code"),
                    zip_code=merged.get("zip"),
                    phone=merged.get("phone"),
                    first_name=merged.get("first_name"),
                    last_name=merged.get("last_name"),
                )
            update_company_location(
                shop_domain,
                access_token,
                location_gid=shopify_location_id,
                name=merged["label"] if name_changed else None,
                phone=merged.get("phone") if phone_changed else None,
                address=address_input,
            )

        with get_session() as session:
            row = session.get(CompanyAddress, uuid.UUID(address_id))
            if not row:
                raise ValueError(f"Address '{address_id}' not found")
            if data.get("is_primary") is True:
                session.execute(
                    update(CompanyAddress)
                    .where(CompanyAddress.company_id == uuid.UUID(company_id))
                    .where(CompanyAddress.id != uuid.UUID(address_id))
                    .values(is_primary=False)
                )
            for k, v in data.items():
                setattr(row, k, v)
            session.commit()
            session.refresh(row)
            result = row.to_dict()

        logger.info(
            "CompanyAddress actualizada",
            extra={"id": address_id, "company_id": company_id},
        )

        if contact_email and shopify_location_id:
            if shop_domain is None or access_token is None:
                shop_domain, access_token = _resolve_shop_and_token()
            with get_session() as session:
                company = session.get(Company, uuid.UUID(company_id))
                shopify_company_id = company.shopify_company_id if company else None
            if shopify_company_id:
                self._link_contact(
                    company_id=company_id,
                    shopify_company_id=shopify_company_id,
                    location_gid=shopify_location_id,
                    contact_email=contact_email,
                    contact_name=contact_name,
                    shop_domain=shop_domain,
                    access_token=access_token,
                    result=result,
                )

        return result

    def delete_address(self, company_id: str, address_id: str) -> bool:
        self._validate_uuid(company_id)
        self._validate_uuid(address_id)
        with get_session() as session:
            row = session.get(CompanyAddress, uuid.UUID(address_id))
            if not row or str(row.company_id) != company_id:
                raise ValueError(f"Address '{address_id}' not found")
            if row.is_primary:
                raise ValueError(
                    "No se puede eliminar la sucursal principal. "
                    "Marca otra sucursal como principal antes de eliminarla."
                )
            shopify_location_id = row.shopify_location_id

        if shopify_location_id:
            shop_domain, access_token = _resolve_shop_and_token()
            delete_company_location(shop_domain, access_token, shopify_location_id)

        with get_session() as session:
            row = session.get(CompanyAddress, uuid.UUID(address_id))
            if row:
                session.delete(row)
                session.commit()

        logger.info(
            "CompanyAddress eliminada",
            extra={"id": address_id, "company_id": company_id},
        )
        return True

    # ------------------------------------------------------------------
    # Cliente asociado a la sucursal
    # ------------------------------------------------------------------
    @staticmethod
    def _link_contact(
        *,
        company_id: str,
        shopify_company_id: str,
        location_gid: str,
        contact_email: str,
        contact_name: str | None,
        shop_domain: str,
        access_token: str,
        result: dict[str, Any],
    ) -> None:
        """Crea o reutiliza el `Client`/Customer del email indicado y lo asigna
        a esta sucursal. Best-effort: si falla, no revierte la sucursal (ya es
        válida por sí sola) — solo se anota `contact_warning` en el resultado."""
        try:
            contact = ContactService().find_or_create_contact(
                company_id, {"email": contact_email, "name": contact_name}
            )
            customer_id = contact.get("shopify_customer_id")
            if not customer_id:
                raise ValueError("El cliente no quedó enlazado a un Customer de Shopify")

            contact_gid = ContactService._resolve_contact_gid(
                shop_domain,
                access_token,
                shopify_company_id=shopify_company_id,
                shopify_customer_id=customer_id,
            )
            if not contact_gid:
                raise ValueError("No se pudo resolver el CompanyContact en Shopify")

            role_id = get_ordering_only_role_id(
                shop_domain, access_token, shopify_company_id
            )
            assign_contact_to_location(
                shop_domain,
                access_token,
                contact_gid=contact_gid,
                role_gid=role_id,
                location_gid=location_gid,
            )
            result["contact"] = contact
        except Exception as e:
            logger.warning(
                "AddressService: no se pudo asociar el cliente a la sucursal — %s",
                e,
                extra={"company_id": company_id, "location_gid": location_gid},
            )
            result["contact_warning"] = str(e)

    # ------------------------------------------------------------------
    # Catálogo B2B
    # ------------------------------------------------------------------
    @staticmethod
    def _add_to_catalog(
        shop_domain: str,
        access_token: str,
        company_type: str | None,
        location_gids: list[str],
    ) -> None:
        segment = COMPANY_TYPE_TO_SEGMENT.get(company_type or "")
        if not segment:
            return
        with get_session() as session:
            seg = session.scalar(
                select(ShopifyPriceSegment).where(
                    ShopifyPriceSegment.segment == segment
                )
            )
            catalog_gid = seg.catalog_gid if seg else None
        if not catalog_gid:
            logger.warning(
                "AddressService: catálogo de %s no existe; sucursal sin precios B2B",
                segment,
            )
            return
        _catalog_context_update(
            shop_domain, access_token, catalog_gid, add=location_gids
        )

    # ------------------------------------------------------------------
    # Validación
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_uuid(value: str) -> None:
        try:
            uuid.UUID(value)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid id (expected UUID): '{value}'") from e

    @staticmethod
    def _validate_payload(
        payload: dict[str, Any], *, require_address: bool
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Body must be a JSON object")

        allowed = {
            "label",
            "is_primary",
            "address1",
            "address2",
            "city",
            "zone_code",
            "country_code",
            "zip",
            "phone",
            "first_name",
            "last_name",
            # Cliente asociado a la sucursal (no persiste en CompanyAddress; ver
            # AddressService._link_contact — se extraen del payload validado antes
            # de tocar columnas del modelo).
            "contact_email",
            "contact_name",
        }
        clean: dict[str, Any] = {}
        for k, v in payload.items():
            if k not in allowed:
                continue
            if k == "is_primary":
                clean[k] = bool(v)
            elif v is None:
                # Permite enviar null para limpiar campos opcionales en update.
                if k in {
                    "address2",
                    "zone_code",
                    "zip",
                    "phone",
                    "first_name",
                    "last_name",
                    "contact_email",
                    "contact_name",
                }:
                    clean[k] = None
            elif k == "contact_email":
                s = str(v).strip().lower()
                if s:
                    clean[k] = s
            else:
                s = str(v).strip()
                if s:
                    clean[k] = s

        if "country_code" in clean:
            clean["country_code"] = clean["country_code"].upper()[:2]

        if require_address:
            required = ("label", "address1", "city")
            missing = [f for f in required if not clean.get(f)]
            if missing:
                raise ValueError(
                    f"Campos obligatorios faltantes: {', '.join(missing)}"
                )
            clean.setdefault("country_code", "CL")

        return clean
