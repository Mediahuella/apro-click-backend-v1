"""Autenticación en $connect: staff (Cognito Bearer) o storefront (api_key en query)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from sqlalchemy import select

service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))
if "/var/task" not in sys.path:
    sys.path.insert(0, "/var/task")

for _p in [Path("/var/task/shared"), service_root / "shared"]:
    if _p.exists() and (_p / "database").exists():
        if str(_p) not in sys.path:
            sys.path.insert(0, str(_p))
        break

from database.engine import get_session  # noqa: E402
from database.models.user import User  # noqa: E402

logger = Logger()

_COGNITO = boto3.client("cognito-idp")
_STAFF_ROLES = frozenset({"SUPERADMIN", "ADMIN", "SALES"})


def authenticate_connection(query_params: dict[str, str]) -> dict[str, Any]:
    """
    Retorna {"sender_type": "USER"|"CLIENT", "actor_id": str}.
    Lanza PermissionError si las credenciales son inválidas.

    - sender_type=USER  → staff vía Cognito Access Token (?token=...).
    - sender_type=CLIENT → cliente del storefront (valida api_key en query params).
    """
    sender_type = (query_params.get("sender_type") or "USER").upper()

    if sender_type == "CLIENT":
        _validate_storefront_key(query_params)
        client_id = (query_params.get("client_id") or "").strip()
        if not client_id:
            raise PermissionError("client_id requerido para sender_type=CLIENT")
        return {"sender_type": "CLIENT", "actor_id": client_id}

    # sender_type == "USER" (staff)
    token = (query_params.get("token") or "").strip()
    if not token:
        raise PermissionError("Se requiere ?token=<AccessToken de Cognito>")

    try:
        r = _COGNITO.get_user(AccessToken=token)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NotAuthorizedException", "UserNotFoundException"):
            raise PermissionError("Token Cognito inválido o expirado") from e
        raise

    sub = next(
        (a["Value"] for a in r.get("UserAttributes", []) if a["Name"] == "sub"),
        None,
    )
    if not sub:
        raise PermissionError("Token sin atributo sub")

    with get_session() as session:
        user = session.scalar(select(User).where(User.cognito_sub == sub))

    if not user or user.status != "ACTIVE":
        raise PermissionError("Usuario inactivo o no registrado")
    if user.role not in _STAFF_ROLES:
        raise PermissionError("Rol sin acceso al chat")

    return {"sender_type": "USER", "actor_id": str(user.id)}


def _validate_storefront_key(query_params: dict[str, str]) -> None:
    expected = (os.environ.get("CHAT_STOREFRONT_API_KEY") or "").strip()
    if not expected:
        return
    got = (query_params.get("api_key") or "").strip()
    if not got or got != expected:
        raise PermissionError("API key de storefront inválida o ausente")
