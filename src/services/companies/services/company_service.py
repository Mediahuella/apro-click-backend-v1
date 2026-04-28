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
from sqlalchemy import delete, update

logger = Logger()

VALID_COMPANY_TYPES = {"SMALL", "MEDIUM", "BIG"}
VALID_PAYMENT_TYPES = {"DIRECT", "CREDIT"}


class CompanyService:
    def create_company(
        self,
        *,
        name: str,
        company_type: str,
        payment_type: str,
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
            return company.to_dict() if company else None

    def list_companies(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        from sqlalchemy import select

        with get_session() as session:
            stmt = (
                select(Company)
                .order_by(Company.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            companies = [c.to_dict() for c in session.scalars(stmt)]
        return {"companies": companies}

    def update_company(
        self, company_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        self._validate_uuid(company_id)

        allowed = {"name", "company_type", "payment_type"}
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

        with get_session() as session:
            company = session.get(Company, uuid.UUID(company_id))
            if not company:
                raise ValueError(f"Company '{company_id}' not found")

            for k, v in filtered.items():
                setattr(company, k, v)
            session.commit()
            session.refresh(company)
            return company.to_dict()

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
