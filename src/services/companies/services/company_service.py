"""Company business-logic service — PostgreSQL via SQLAlchemy."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger

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
from database.models.client import Client
from database.models.company import Company
from database.models.conversation import Conversation
from database.models.registration_request import CompanyRegistrationRequest
from database.models.user import User
from database.models.user_company import UserCompany
from sqlalchemy import delete, select, update, or_
from sqlalchemy.orm import Session
from utils.shopify_catalog_sync import sync_company_catalog

logger = Logger()

VALID_COMPANY_TYPES = {"SMALL", "MEDIUM", "BIG"}
VALID_PAYMENT_TYPES = {"DIRECT", "CREDIT"}


def _attach_assigned_sales_user(
    session: Session, companies: list[dict[str, Any]]
) -> None:
    """Añade ``assigned_sales_user`` (dict o None) a cada dict de empresa.

    Toma el vendedor SALES vinculado vía ``user_companies`` para esa empresa.
    Si hay más de uno (dato legado, el modelo M2M lo permite), se queda con el
    de menor ``user_id`` para que el resultado sea determinista.
    """
    if not companies:
        return
    company_ids = [uuid.UUID(c["id"]) for c in companies]
    rows = session.execute(
        select(
            UserCompany.company_id,
            User.id,
            User.given_name,
            User.family_name,
            User.email,
        )
        .join(User, User.id == UserCompany.user_id)
        .where(UserCompany.company_id.in_(company_ids), User.role == "SALES")
        .order_by(UserCompany.company_id, User.id)
    ).all()
    by_company: dict[str, dict[str, Any]] = {}
    for company_id, user_id, given_name, family_name, email in rows:
        key = str(company_id)
        if key in by_company:
            continue
        by_company[key] = {
            "id": str(user_id),
            "given_name": given_name,
            "family_name": family_name,
            "email": email,
        }
    for c in companies:
        c["assigned_sales_user"] = by_company.get(c["id"])


class CompanyService:
    def get_company_by_shopify_id(
        self, shopify_company_id: str
    ) -> dict[str, Any] | None:
        """Resuelve una ``Company`` local por su ``shopify_company_id``.

        Usado por los endpoints públicos del storefront (canal ``X-Api-Key``):
        el cliente final solo conoce el GID de Shopify de su propia sesión B2B,
        nunca el UUID interno.
        """
        shopify_company_id = str(shopify_company_id or "").strip()
        if not shopify_company_id:
            return None
        with get_session() as session:
            company = session.scalar(
                select(Company).where(
                    Company.shopify_company_id == shopify_company_id
                )
            )
            return company.to_dict() if company else None

    def create_company(
        self,
        *,
        name: str,
        company_type: str,
        payment_type: str,
        billing_rut: str | None = None,
        billing_razon_social: str | None = None,
        billing_giro: str | None = None,
        billing_region: str | None = None,
        billing_direccion: str | None = None,
    ) -> dict[str, Any]:
        if not name or not str(name).strip():
            raise ValueError("'name' is required")

        ct = self._validate_company_type(company_type)
        pt = self._validate_payment_type(payment_type)

        with get_session() as session:
            company = Company(
                name=name.strip(),
                company_type=ct,
                payment_type=pt,
                billing_rut=(billing_rut or "").strip() or None,
                billing_razon_social=(billing_razon_social or "").strip() or None,
                billing_giro=(billing_giro or "").strip() or None,
                billing_region=(billing_region or "").strip() or None,
                billing_direccion=(billing_direccion or "").strip() or None,
            )
            session.add(company)
            session.commit()
            session.refresh(company)
            result = company.to_dict()

        logger.info("Company created", extra={"id": result["id"]})
        return result

    def get_company(self, company_id: str) -> dict[str, Any] | None:
        self._validate_uuid(company_id)
        with get_session() as session:
            company = session.get(Company, uuid.UUID(company_id))
            if not company:
                return None
            result = company.to_dict()
            _attach_assigned_sales_user(session, [result])
            return result

    def list_companies(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        assigned_user_id: str | None = None,
        referred_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Lista empresas con dos filtros mutuamente compatibles:

        - `assigned_user_id`: cartera amplia — empresas vinculadas vía `UserCompany` M2M
          o por `companies.referred_by_user_id` (incluye asignaciones manuales del admin).
        - `referred_by_user_id`: estricto — sólo empresas captadas por el link de
          invitación del vendedor (`companies.referred_by_user_id == X`). Para el detalle
          del KPI de referidos donde no querés mezclar con asignación manual.
        """
        from sqlalchemy import select

        def _parse(name: str, raw: str | None) -> uuid.UUID | None:
            if not raw:
                return None
            try:
                return uuid.UUID(raw)
            except (ValueError, TypeError) as e:
                raise ValueError(f"{name} inválido (UUID): {raw!r}") from e

        assigned_uuid = _parse("assigned_user_id", assigned_user_id)
        referred_uuid = _parse("referred_by_user_id", referred_by_user_id)

        with get_session() as session:
            stmt = select(Company)
            if assigned_uuid is not None:
                stmt = (
                    stmt.outerjoin(
                        UserCompany, UserCompany.company_id == Company.id
                    )
                    .where(
                        or_(
                            UserCompany.user_id == assigned_uuid,
                            Company.referred_by_user_id == assigned_uuid,
                        )
                    )
                    .distinct()
                )
            if referred_uuid is not None:
                stmt = stmt.where(Company.referred_by_user_id == referred_uuid)
            stmt = stmt.order_by(Company.created_at.desc()).limit(limit).offset(offset)
            companies = [c.to_dict() for c in session.scalars(stmt)]
            _attach_assigned_sales_user(session, companies)
        return {"companies": companies}

    def set_assigned_user(
        self, company_id: str, user_id: str | None
    ) -> dict[str, Any]:
        """Reemplaza el vendedor SALES asignado a la empresa (asignación única).

        `user_id=None` desasigna. Reemplaza por completo las filas de
        `user_companies` de esta empresa: si hoy hay más de un vendedor
        vinculado (dato legado), queda solo el elegido.
        """
        self._validate_uuid(company_id)
        if user_id is not None:
            self._validate_uuid(user_id)

        with get_session() as session:
            company = session.get(Company, uuid.UUID(company_id))
            if not company:
                raise ValueError(f"Company '{company_id}' not found")

            if user_id is not None:
                user = session.get(User, uuid.UUID(user_id))
                if not user:
                    raise ValueError(f"User '{user_id}' not found")
                if user.role != "SALES":
                    raise ValueError(f"User '{user_id}' no tiene rol SALES")

            session.execute(
                delete(UserCompany).where(UserCompany.company_id == company.id)
            )
            if user_id is not None:
                session.add(
                    UserCompany(user_id=uuid.UUID(user_id), company_id=company.id)
                )
            session.commit()
            session.refresh(company)

            result = company.to_dict()
            _attach_assigned_sales_user(session, [result])

        logger.info(
            "Company assigned user updated",
            extra={"company_id": company_id, "user_id": user_id},
        )
        return result

    def update_company(
        self, company_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_uuid(company_id)

        allowed = {
            "name",
            "company_type",
            "payment_type",
            "billing_rut",
            "billing_razon_social",
            "billing_giro",
            "billing_region",
            "billing_direccion",
        }
        filtered: dict[str, Any] = {}
        for k, v in updates.items():
            if k not in allowed or v is None:
                continue
            if k == "name":
                filtered[k] = str(v).strip()
            elif k == "company_type":
                filtered[k] = self._validate_company_type(str(v))
            elif k == "payment_type":
                filtered[k] = self._validate_payment_type(str(v))
            elif k in (
                "billing_rut",
                "billing_razon_social",
                "billing_giro",
                "billing_region",
                "billing_direccion",
            ):
                s = str(v).strip()
                if s:
                    filtered[k] = s

        with get_session() as session:
            company = session.get(Company, uuid.UUID(company_id))
            if not company:
                raise ValueError(f"Company '{company_id}' not found")

            old_company_type = company.company_type
            shopify_company_id = company.shopify_company_id

            for k, v in filtered.items():
                setattr(company, k, v)
            session.commit()
            session.refresh(company)
            result = company.to_dict()

        # Sincronizar catálogos B2B en Shopify si cambió el tipo de empresa.
        new_company_type = filtered.get("company_type")
        if new_company_type and new_company_type != old_company_type:
            sync_company_catalog(shopify_company_id, old_company_type, new_company_type)

        return result

    def delete_company(self, company_id: str) -> bool:
        self._validate_uuid(company_id)
        with get_session() as session:
            company = session.get(Company, uuid.UUID(company_id))
            if not company:
                raise ValueError(f"Company '{company_id}' not found")
            if company.is_system:
                raise ValueError("No se puede eliminar la empresa del sistema")

            cid = company.id
            # Orden: conversaciones (messages → CASCADE), clientes, FKs opcionales, empresa.
            # Sin esto, SQLAlchemy intenta SET company_id=NULL en clients (NOT NULL) y falla.
            session.execute(delete(Conversation).where(Conversation.company_id == cid))
            session.execute(delete(Client).where(Client.company_id == cid))
            session.execute(update(User).where(User.company_id == cid).values(company_id=None))
            session.execute(
                update(CompanyRegistrationRequest)
                .where(CompanyRegistrationRequest.resolved_company_id == cid)
                .values(resolved_company_id=None)
            )
            session.delete(company)
            session.commit()
        logger.info("Company deleted", extra={"id": company_id})
        return True

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_uuid(value: str) -> None:
        try:
            uuid.UUID(value)
        except ValueError as e:
            raise ValueError(
                f"Invalid company id (expected UUID): '{value}'"
            ) from e

    @staticmethod
    def _validate_company_type(value: str) -> str:
        v = value.strip().upper()
        if v not in VALID_COMPANY_TYPES:
            raise ValueError(
                f"Invalid company_type '{value}'. Valid: {sorted(VALID_COMPANY_TYPES)}"
            )
        return v

    @staticmethod
    def _validate_payment_type(value: str) -> str:
        v = value.strip().upper()
        if v not in VALID_PAYMENT_TYPES:
            raise ValueError(
                f"Invalid payment_type '{value}'. Valid: {sorted(VALID_PAYMENT_TYPES)}"
            )
        return v
