"""Auth ligera para el servicio shipping.

Valida el Access Token de Cognito (``GetUser`` + ``AdminListGroupsForUser``)
sin tocar Postgres. Sirve para endpoints de cotización donde sólo importa
"el usuario está autenticado y tiene un rol del staff".
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import boto3
from botocore.exceptions import ClientError

ALLOWED_ROLES = frozenset({"SUPERADMIN", "ADMIN", "SALES", "KPI_VISUALIZERS"})


@lru_cache(maxsize=1)
def _idp():
    return boto3.client("cognito-idp")


def _user_pool_id() -> str:
    return (os.environ.get("COGNITO_USER_POOL_ID") or "").strip()


def parse_bearer_authorization(headers: dict[str, str] | None) -> str | None:
    if not headers:
        return None
    auth = None
    for k, v in headers.items():
        if k.lower() == "authorization":
            auth = v
            break
    if not auth:
        return None
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def authenticate(access_token: str) -> dict[str, Any]:
    """Devuelve ``{username, sub, email, role}`` o lanza ``PermissionError``.

    ``role`` es el primer grupo de Cognito en mayúsculas (precedence más alto).
    Si el usuario no pertenece a ningún grupo permitido, lanza PermissionError.
    """
    try:
        r = _idp().get_user(AccessToken=access_token)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NotAuthorizedException", "UserNotFoundException"):
            raise PermissionError("Token inválido o expirado") from e
        raise

    username = r.get("Username") or ""
    attrs = {a.get("Name"): a.get("Value") for a in r.get("UserAttributes") or []}
    sub = attrs.get("sub") or ""
    email = attrs.get("email") or ""

    pool_id = _user_pool_id()
    if not pool_id:
        raise PermissionError("COGNITO_USER_POOL_ID no configurado")

    try:
        groups_res = _idp().admin_list_groups_for_user(
            UserPoolId=pool_id, Username=username
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("UserNotFoundException", "NotAuthorizedException"):
            raise PermissionError("Usuario sin acceso al pool") from e
        raise

    role = ""
    for g in groups_res.get("Groups") or []:
        name = (g.get("GroupName") or "").upper()
        if name in ALLOWED_ROLES:
            role = name
            break

    if not role:
        raise PermissionError("Usuario sin rol válido para shipping")

    return {
        "username": username,
        "sub": sub,
        "email": email,
        "role": role,
    }
