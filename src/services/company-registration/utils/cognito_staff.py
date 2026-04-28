"""Valida Access Token de Cognito y resuelve el usuario de negocio en PostgreSQL."""
from __future__ import annotations

import sys
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

# Roles que pueden revisar solicitudes (panel / aprobar / rechazar)
STAFF_ROLES_APPROVER = frozenset({"SUPERADMIN", "ADMIN", "SALES"})


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
    return d


def require_approver(user: dict[str, Any]) -> None:
    if user.get("role") not in STAFF_ROLES_APPROVER:
        raise PermissionError("Sin permisos para gestionar solicitudes")
