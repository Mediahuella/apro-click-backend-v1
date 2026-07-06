"""Cognito + reglas de acceso a cotizaciones por rol y empresa.

Reusa el mismo patrón que ``orders`` (resolución de usuario por Access Token,
alcance por empresas vía ``order_company_ids``). Lectura: staff con visibilidad
por empresa; negociación (crear draft / invoice / complete): sin KPI.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import select

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
from database.models.user import User  # noqa: E402
from database.user_context import attach_order_company_ids  # noqa: E402

COGNITO_IDP = boto3.client("cognito-idp")

READ_ROLES = frozenset({"SUPERADMIN", "ADMIN", "SALES", "KPI_VISUALIZERS"})
NEGOTIATE_ROLES = frozenset({"SUPERADMIN", "ADMIN", "SALES"})


def parse_bearer_authorization(headers: dict[str, str] | None) -> str | None:
    if not headers:
        return None
    auth = headers.get("authorization") or headers.get("Authorization")
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def get_user_by_cognito_access_token(access_token: str) -> dict[str, Any]:
    """Resuelve el usuario de negocio por Access Token de Cognito (GetUser)."""
    try:
        r = COGNITO_IDP.get_user(AccessToken=access_token)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NotAuthorizedException", "UserNotFoundException"):
            raise PermissionError("Token inválido o expirado") from e
        raise

    sub: str | None = None
    for attr in r.get("UserAttributes", []):
        if attr.get("Name") == "sub":
            sub = attr.get("Value")
            break
    if not sub:
        raise PermissionError("Token sin sub de Cognito")

    with get_session() as session:
        row = session.scalar(select(User).where(User.cognito_sub == sub))
        if not row:
            raise PermissionError("Usuario no registrado en la aplicación")
        if row.status != "ACTIVE":
            raise PermissionError("Usuario inactivo")
        d = row.to_dict()
        d["id"] = str(d["id"])
        if d.get("company_id") is not None:
            d["company_id"] = str(d["company_id"])
        attach_order_company_ids(session, row, d)
    if not isinstance(d.get("order_company_ids"), list):
        d["order_company_ids"] = []
    return d


def is_platform_admin(user: dict[str, Any]) -> bool:
    return user.get("role") in ("SUPERADMIN", "ADMIN")


def accessible_company_uuids(user: dict[str, Any]) -> set[uuid.UUID]:
    out: set[uuid.UUID] = set()
    raw = user.get("order_company_ids")
    if not isinstance(raw, (list, tuple)):
        return out
    for x in raw:
        try:
            out.add(uuid.UUID(str(x)))
        except ValueError:
            continue
    return out


def can_read_quote(user: dict[str, Any], company_id: uuid.UUID | None) -> bool:
    if user.get("role") not in READ_ROLES:
        return False
    if is_platform_admin(user):
        return True
    if company_id is None:
        return False
    return company_id in accessible_company_uuids(user)
