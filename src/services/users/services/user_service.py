"""User business-logic service — orchestrates Cognito + PostgreSQL."""
from __future__ import annotations

import os
import sys
import uuid as uuid_mod
from typing import Any

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from pathlib import Path

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
    if path.exists() and (path / "cognito").exists():
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        break

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from database.models.company import Company
from database.models.user_company import UserCompany
from database.user_context import attach_order_company_ids
from database.models.user import (
    User,
    VALID_ROLES,
    VALID_STATUSES,
    ROLE_TO_COGNITO_GROUP,
    coerce_role,
    coerce_status,
)
from database.engine import get_session
from cognito.client import (
    admin_create_user,
    admin_get_user,
    admin_update_user_attributes,
    admin_disable_user,
    admin_enable_user,
    admin_delete_user,
    admin_add_user_to_group,
    admin_remove_user_from_group,
    admin_list_groups_for_user,
)
from utils.company_ids import company_ids_in_update
from utils.shopify_staff_link import (
    associate_shopify_staff_by_gid,
    try_link_shopify_staff_for_user,
)

logger = Logger()

_COGNITO_GROUP_TO_ROLE: dict[str, str] = {v: k for k, v in ROLE_TO_COGNITO_GROUP.items()}


def _cognito_group_to_role(group_name: str) -> str:
    return _COGNITO_GROUP_TO_ROLE.get(group_name, group_name.upper())


def _sub_from_cognito_attribute_list(
    attributes: list[dict[str, Any]],
) -> str:
    for attr in attributes:
        if attr.get("Name") == "sub" and attr.get("Value") is not None:
            return str(attr["Value"])
    raise ValueError("Cognito: faltan atributos: no hay 'sub'")


def _delete_cognito_user_silent_after_failed_db(username: str) -> None:
    try:
        admin_delete_user(username)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "UserNotFoundException":
            return
        logger.exception(
            "Cognito: no se pudo eliminar el usuario tras fallo en base de datos",
            extra={"username": username},
        )


def _remove_user_from_cognito_pool(email: str, cognito_sub: str) -> None:
    """Elimina el usuario en el User Pool (AdminDeleteUser) antes de borrar la fila en PostgreSQL.

    Intenta con ``email`` (Username usado al crear) y, si Cognito responde que no existe,
    con ``cognito_sub`` por posibles cuentas heredadas. Cualquier otro error de Cognito
    hace fallar la operación para no dejar un usuario activo en el pool mientras se borra en BD.
    """
    for label, username in (("email", email), ("cognito_sub", cognito_sub)):
        if not username or (label == "cognito_sub" and username == email):
            continue
        try:
            admin_delete_user(username)
            logger.info(
                "Usuario eliminado de Cognito (AdminDeleteUser)",
                extra={"cognito_key": label, "email": email, "cognito_sub": cognito_sub},
            )
            return
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "UserNotFoundException":
                raise
    logger.warning(
        "Cognito: usuario no encontrado por email ni sub; se elimina la fila en base de datos",
        extra={"email": email, "cognito_sub": cognito_sub},
    )


def _verify_cognito_user_and_group(
    email: str,
    expected_sub: str,
    expected_group: str,
) -> None:
    try:
        fetched = admin_get_user(email)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "UserNotFoundException":
            raise ValueError(
                "Cognito: el usuario no aparece al verificar (AdminGetUser) "
                "tras AdminCreateUser; revise el pool o reintente."
            ) from e
        raise
    read_sub = _sub_from_cognito_get_user(fetched)
    if read_sub != expected_sub:
        raise ValueError(
            "Cognito: el sub no coincide al verificar el usuario creado "
            f"(create={expected_sub!r}, get={read_sub!r})"
        )
    group_names = {g["GroupName"] for g in admin_list_groups_for_user(email)}
    if expected_group not in group_names:
        raise ValueError(
            f"Cognito: el usuario se creó pero no está en el grupo '{expected_group}' "
            f"(grupos actuales: {sorted(group_names)!r})"
        )


def _sub_from_cognito_get_user(fetched: dict[str, Any]) -> str:
    return _sub_from_cognito_attribute_list(fetched.get("UserAttributes", []))


def _normalize_codigo_sap(raw: object | None) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s if s else None


def _resolve_user(session: Session, user_id: str) -> User | None:
    """Resolve by PostgreSQL `users.id` (UUID) or by `cognito_sub` (Cognito)."""
    try:
        uid = uuid_mod.UUID(user_id)
    except ValueError:
        uid = None
    if uid is not None:
        by_id = session.get(User, uid)
        if by_id is not None:
            return by_id
    return session.execute(
        select(User).where(User.cognito_sub == user_id)
    ).scalar_one_or_none()


def _apro_click_company_id(session: Session) -> uuid_mod.UUID:
    """Empresa plataforma Apro Click: fila `companies` con `is_system = true`."""
    row = session.scalars(
        select(Company)
        .where(Company.is_system.is_(True))
        .order_by(Company.id.asc())
        .limit(1)
    ).first()
    if row is not None:
        return row.id

    raw = os.environ.get("APRO_CLICK_COMPANY_ID", "").strip()
    if raw:
        try:
            cid = uuid_mod.UUID(raw)
        except ValueError as e:
            raise ValueError(
                "APRO_CLICK_COMPANY_ID debe ser un UUID (companies.id) válido"
            ) from e
        if session.get(Company, cid) is None:
            raise ValueError(
                "APRO_CLICK_COMPANY_ID no existe en la tabla companies"
            )
        return cid

    raise ValueError(
        "No hay empresa plataforma: ninguna fila en companies con is_system = true. "
        "Marque la empresa de Apro Click en la BD o defina APRO_CLICK_COMPANY_ID."
    )


def _replace_user_order_companies(
    session: Session, user_id: uuid_mod.UUID, company_ids: list[str]
) -> None:
    """Sustituye filas en `user_companies` (lista vacía = ninguna empresa)."""
    session.execute(delete(UserCompany).where(UserCompany.user_id == user_id))
    seen: set[uuid_mod.UUID] = set()
    for raw in company_ids:
        s = (str(raw) if raw is not None else "").strip()
        if not s:
            continue
        try:
            cid = uuid_mod.UUID(s)
        except ValueError as e:
            raise ValueError(f"company_id inválido: {raw!r}") from e
        if cid in seen:
            continue
        seen.add(cid)
        if session.get(Company, cid) is None:
            raise ValueError(f"Empresa no encontrada: {s}")
        session.add(UserCompany(user_id=user_id, company_id=cid))


class UserService:
    """Manages user lifecycle across Cognito and PostgreSQL."""

    def create_user(
        self,
        email: str,
        given_name: str,
        family_name: str,
        role: str = "SALES",
        temporary_password: str | None = None,
        company_ids: list[str] | None = None,
        codigo_sap: str | None = None,
    ) -> dict[str, Any]:
        role = coerce_role(role)
        if role not in VALID_ROLES:
            raise ValueError(f"Invalid role '{role}'. Valid: {sorted(VALID_ROLES)}")
        sap_n = _normalize_codigo_sap(codigo_sap)
        if role == "SALES":
            if not sap_n:
                raise ValueError(
                    "codigo_sap es obligatorio cuando el rol es SALES "
                    "(valor de texto no vacío)"
                )
        else:
            sap_n = None

        with get_session() as session:
            existing = session.execute(
                select(User).where(User.email == email)
            ).scalar_one_or_none()
            if existing:
                raise ValueError(f"User with email '{email}' already exists")

        cognito_attrs = {"given_name": given_name, "family_name": family_name}
        cognito_created = False
        try:
            cognito_response = admin_create_user(
                email=email,
                temporary_password=temporary_password,
                attributes=cognito_attrs,
            )
            cognito_created = True
            cognito_user = cognito_response["User"]
            sub = _sub_from_cognito_attribute_list(cognito_user["Attributes"])

            expected_group = ROLE_TO_COGNITO_GROUP[role]
            admin_add_user_to_group(email, expected_group)
            _verify_cognito_user_and_group(email, sub, expected_group)
        except Exception:
            if cognito_created:
                _delete_cognito_user_silent_after_failed_db(email)
            raise

        try:
            with get_session() as session:
                platform_company_id = _apro_click_company_id(session)
                user = User(
                    cognito_sub=sub,
                    email=email,
                    given_name=given_name,
                    family_name=family_name,
                    role=role,
                    status="PENDING",
                    company_id=platform_company_id,
                    codigo_sap=sap_n,
                )
                session.add(user)
                session.flush()
                if company_ids is not None:
                    _replace_user_order_companies(session, user.id, company_ids)
                session.commit()
                session.refresh(user)
                result = user.to_dict()
                attach_order_company_ids(session, user, result)
        except Exception:
            logger.exception(
                "Error guardando el usuario en base de datos tras crear en Cognito; "
                "revirtiendo el usuario de Cognito",
                extra={"email": email, "cognito_sub": sub},
            )
            _delete_cognito_user_silent_after_failed_db(email)
            raise

        logger.info("User created", extra={"id": result["id"], "email": email, "role": role})
        return result

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with get_session() as session:
            user = _resolve_user(session, user_id)
            if not user:
                return None

            result = user.to_dict()
            attach_order_company_ids(session, user, result)

        groups = admin_list_groups_for_user(result["email"])
        result["cognito_groups"] = [
            _cognito_group_to_role(g["GroupName"]) for g in groups
        ]
        return result

    def list_users(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        role: str | None = None,
    ) -> dict[str, Any]:
        with get_session() as session:
            stmt = (
                select(User)
                .order_by(User.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            if role:
                stmt = stmt.where(User.role == role.upper())
            rows = list(session.scalars(stmt).all())
            users: list[dict[str, Any]] = []
            for u in rows:
                d = u.to_dict()
                attach_order_company_ids(session, u, d)
                users.append(d)
        return {"users": users}

    def update_user(self, user_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with get_session() as session:
            user = _resolve_user(session, user_id)
            if not user:
                raise ValueError(f"User '{user_id}' not found")
            prior_role = user.role

            cognito_attr_updates: dict[str, str] = {}
            if updates.get("given_name"):
                cognito_attr_updates["given_name"] = updates["given_name"]
                user.given_name = updates["given_name"]
            if updates.get("family_name"):
                cognito_attr_updates["family_name"] = updates["family_name"]
                user.family_name = updates["family_name"]

            if cognito_attr_updates:
                admin_update_user_attributes(user.email, cognito_attr_updates)

            new_role = updates.get("role")
            if new_role is not None:
                new_role = coerce_role(new_role)
                if new_role not in VALID_ROLES:
                    raise ValueError(f"Invalid role '{new_role}'. Valid: {sorted(VALID_ROLES)}")
                if new_role != user.role:
                    admin_remove_user_from_group(
                        user.email, ROLE_TO_COGNITO_GROUP[user.role]
                    )
                    admin_add_user_to_group(
                        user.email, ROLE_TO_COGNITO_GROUP[new_role]
                    )
                    user.role = new_role

            new_status = updates.get("status")
            if new_status is not None:
                new_status = coerce_status(new_status)
                if new_status not in VALID_STATUSES:
                    raise ValueError(f"Invalid status '{new_status}'. Valid: {sorted(VALID_STATUSES)}")
                if new_status != user.status:
                    if new_status == "DISABLED":
                        admin_disable_user(user.email)
                    elif new_status == "ACTIVE":
                        admin_enable_user(user.email)
                    user.status = new_status

            present, cids = company_ids_in_update(updates)
            if present:
                _replace_user_order_companies(session, user.id, cids)

            if "codigo_sap" in updates:
                user.codigo_sap = _normalize_codigo_sap(updates.get("codigo_sap"))

            transitioned_to_sales = prior_role != "SALES" and user.role == "SALES"
            if user.role != "SALES":
                user.codigo_sap = None
            elif transitioned_to_sales and not user.codigo_sap:
                raise ValueError(
                    "codigo_sap es obligatorio al asignar el rol SALES "
                    "(incluya 'codigo_sap' en el cuerpo con un valor no vacío)"
                )
            elif "codigo_sap" in updates and not user.codigo_sap:
                raise ValueError(
                    "codigo_sap no puede estar vacío para usuarios con rol SALES"
                )

            session.commit()
            session.refresh(user)
            out = user.to_dict()
            attach_order_company_ids(session, user, out)
            return out

    def delete_user(self, user_id: str) -> bool:
        """Borra primero en el User Pool de Cognito y luego el registro en PostgreSQL."""
        with get_session() as session:
            user = _resolve_user(session, user_id)
            if not user:
                raise ValueError(f"User '{user_id}' not found")

            sub = user.cognito_sub
            email = user.email
            _remove_user_from_cognito_pool(email, sub)
            session.delete(user)
            session.commit()

        logger.info("User deleted", extra={"cognito_sub": sub, "user_id": str(user_id)})
        return True

    def link_shopify_staff(self, user_id: str) -> dict[str, Any]:
        """Vincula el usuario con StaffMember en Shopify (mismo email)."""
        with get_session() as session:
            user = _resolve_user(session, user_id)
            if not user:
                raise ValueError(f"User '{user_id}' not found")
            uid = user.id
            email = user.email
            role = user.role
            gn = user.given_name
            fn = user.family_name
        return try_link_shopify_staff_for_user(
            user_id=uid,
            email=email,
            role=role,
            given_name=gn,
            family_name=fn,
        )

    def associate_shopify_staff(self, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Asocia un StaffMember **ya creado en Shopify** vía GID (copiado del Admin)."""
        gid = body.get("shopify_staff_member_gid")
        if gid is None:
            gid = body.get("shopifyStaffMemberGid")
        if not isinstance(gid, str) or not gid.strip():
            raise ValueError(
                "Cuerpo JSON: se requiere 'shopify_staff_member_gid' (GID tipo "
                "gid://shopify/StaffMember/... desde Shopify Admin → Usuarios → copiar / inspector)."
            )
        raw_skip = body.get("skip_email_verification", False)
        skip = raw_skip in (True, "true", 1, "1")

        with get_session() as session:
            user = _resolve_user(session, user_id)
            if not user:
                raise ValueError(f"User '{user_id}' not found")
            uid = user.id
            email = user.email
            role = user.role

        return associate_shopify_staff_by_gid(
            user_id=uid,
            user_email=email,
            role=role,
            staff_member_gid=gid,
            skip_email_verification=skip,
        )
