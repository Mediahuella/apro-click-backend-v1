"""Company business-logic service — PostgreSQL via SQLAlchemy."""
from __future__ import annotations

import re
import sys
import unicodedata
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
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.orm import Session
from utils.shopify_catalog_sync import sync_company_catalog
from utils.shopify_credit_sync import sync_company_credit_metafield

logger = Logger()


def _normalize_rut(value: str | None) -> str:
    """Deja solo dígitos y el dígito verificador ``K`` (sin puntos ni guión)."""
    return re.sub(r"[^0-9kK]", "", value or "").upper()



# Similitud mínima (0-1) para que un nombre mal tipeado cuente como match.
# 0.5 deja pasar ``comerzializadora`` → ``COMERCIALIZADORA`` (0.7) sin traer ruido.
_SIMILARITY_THRESHOLD = 0.5


def _squeeze(expr):
    """Colapsa espacios repetidos en SQL para comparar contra `_normalize_term`."""
    return func.regexp_replace(expr, r"\s+", " ", "g")


def _searchable(expr):
    """Columna lista para comparar: minúsculas, sin acentos y sin espacios dobles."""
    return _squeeze(func.unaccent(func.lower(expr)))


def _normalize_term(value: str) -> str:
    """Mismo tratamiento del lado del término buscado (sin acentos ni espacios dobles)."""
    folded = unicodedata.normalize("NFKD", value or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", folded).strip().lower()


def _rut_sql_expression():
    """``billing_rut`` normalizado en SQL, para comparar contra `_normalize_rut`."""
    expr = func.upper(func.coalesce(Company.billing_rut, ""))
    for separator in (".", "-", " "):
        expr = func.replace(expr, separator, "")
    return expr


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

        Acepta las **dos formas** en que llega ese identificador, porque
        dependen del canal: el theme (Liquid) expone
        ``customer.current_company.id`` **numérico** (``3740172472``) y el
        checkout (``usePurchasingCompany().company.id``) devuelve el **GID**
        completo. En BD se guarda numérico. Sin esta normalización, el canal
        que manda GID recibe 404 aunque la empresa exista.
        """
        shopify_company_id = str(shopify_company_id or "").strip()
        if not shopify_company_id:
            return None
        numerico = shopify_company_id.rsplit("/", 1)[-1] if shopify_company_id.startswith(
            "gid://shopify/Company/"
        ) else shopify_company_id
        gid = f"gid://shopify/Company/{numerico}"
        with get_session() as session:
            company = session.scalar(
                select(Company).where(
                    Company.shopify_company_id.in_([numerico, gid])
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
        q: str | None = None,
        exclude_system: bool = False,
    ) -> dict[str, Any]:
        """Lista empresas con filtros mutuamente compatibles:

        - `assigned_user_id`: cartera amplia — empresas vinculadas vía `UserCompany` M2M
          o por `companies.referred_by_user_id` (incluye asignaciones manuales del admin).
        - `referred_by_user_id`: estricto — sólo empresas captadas por el link de
          invitación del vendedor (`companies.referred_by_user_id == X`). Para el detalle
          del KPI de referidos donde no querés mezclar con asignación manual.
        - `exclude_system`: oculta la empresa interna (`is_system`) para que el
          `total` que pagina el panel coincida con lo que muestra la grilla.
        - `q`: búsqueda por nombre, razón social o RUT. Va en el servidor porque el
          panel filtraba en cliente sobre la página cargada (500 filas) y las
          empresas más antiguas eran inbuscables aunque existieran.
        """
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
            if exclude_system:
                stmt = stmt.where(Company.is_system.is_(False))
            # El término y las columnas se normalizan igual (minúsculas, sin
            # acentos, sin espacios dobles): pegar un nombre con doble espacio
            # (`R Y A  LIMITADA`) o sin tilde no debería perder la empresa.
            term = _normalize_term(q or "")
            similarity = None
            exact_match = None
            if term:
                like = f"%{term}%"
                exact_match = or_(
                    _searchable(Company.name).like(like),
                    _searchable(func.coalesce(Company.billing_razon_social, "")).like(
                        like
                    ),
                )
                conditions = [exact_match]
                # El RUT se guarda con guión (``76217505-3``) pero el usuario lo
                # tipea con puntos o sin nada: comparamos ambos lados sin separadores.
                # Sin dígitos no es un RUT: ``huelpack`` normalizaría a ``K`` y
                # traería por RUT toda empresa con dígito verificador K.
                rut_term = _normalize_rut(term) if any(c.isdigit() for c in term) else ""
                if rut_term:
                    conditions.append(
                        _rut_sql_expression().like(f"%{rut_term}%")
                    )
                # Tolerancia a errores de tipeo (pg_trgm): ``comerzializadora``
                # encuentra ``COMERCIALIZADORA``. Solo para texto —un RUT mal
                # tipeado no debe traer empresas parecidas.
                if not rut_term:
                    similarity = func.word_similarity(term, func.lower(Company.name))
                    conditions.append(similarity >= _SIMILARITY_THRESHOLD)
                stmt = stmt.where(or_(*conditions))
            # Total con los mismos filtros pero sin paginar: el consumidor no
            # puede deducirlo del largo de la página. El KPI del dashboard lo
            # hacía y quedó clavado en el `limit` (500) al pasar las 500 empresas.
            total = int(
                session.scalar(
                    select(func.count()).select_from(stmt.subquery())
                )
                or 0
            )
            if term:
                # Primero las coincidencias literales, después las difusas por
                # similitud; a igual relevancia, la más reciente.
                order = [
                    case((exact_match, 0), else_=1),
                    similarity.desc() if similarity is not None else None,
                    Company.created_at.desc(),
                ]
                stmt = stmt.order_by(*[o for o in order if o is not None])
            else:
                stmt = stmt.order_by(Company.created_at.desc())
            stmt = stmt.limit(limit).offset(offset)
            companies = [c.to_dict() for c in session.scalars(stmt)]
            _attach_assigned_sales_user(session, companies)
        return {"companies": companies, "total": total}

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
            old_payment_type = company.payment_type
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

        # Habilitar/deshabilitar el pago con crédito en el checkout: la Function
        # ``manejo-de-credito`` solo ve el metafield de la Company en Shopify.
        new_payment_type = filtered.get("payment_type")
        if new_payment_type and new_payment_type != old_payment_type:
            sync_company_credit_metafield(shopify_company_id, new_payment_type)

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
