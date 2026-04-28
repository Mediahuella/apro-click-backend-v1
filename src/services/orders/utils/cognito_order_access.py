"""Cognito + reglas: lectura/escritura de pedidos por rol y empresa."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import select
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
from database.models.shopify_order import ShopifyOrder  # noqa: E402
from database.models.user import User  # noqa: E402
from database.user_context import attach_order_company_ids  # noqa: E402

COGNITO_IDP = boto3.client("cognito-idp")

# Lectura: staff con visibilidad por empresa; escritura de intervención: sin KPI.
READ_ROLES = frozenset({"SUPERADMIN", "ADMIN", "SALES", "KPI_VISUALIZERS"})
INTERVENTION_ROLES = frozenset({"SUPERADMIN", "ADMIN", "SALES"})


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
    """
    Resuelve el usuario de negocio por **Access Token** de Cognito (GetUser) y
    enriquece con **order_company_ids** (``user_companies`` + ``users.company_id``
    si hiciera falta completar el alcance).

    Ese arreglo es el alcance de pedidos y chat para roles que no sean
    plataforma (SUPERADMIN/ADMIN). No depende de query params: el listado
    de pedidos filtra con ``ShopifyOrder.company_id IN (order_company_ids)``.
    """
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


def order_accessible_company_uuids(user: dict[str, Any]) -> set[uuid.UUID]:
    """UUIDs en ``user['order_company_ids']`` (M2M + company principal si se unió al adjuntar)."""
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


def can_read_order(user: dict[str, Any], order: ShopifyOrder) -> bool:
    role = user.get("role")
    if role not in READ_ROLES:
        return False
    if is_platform_admin(user):
        return True
    if not order.company_id:
        return False
    return order.company_id in order_accessible_company_uuids(user)


def can_write_intervention(user: dict[str, Any], order: ShopifyOrder) -> bool:
    if user.get("role") not in INTERVENTION_ROLES:
        return False
    if order.internal_status != "PENDING":
        return False
    if is_platform_admin(user):
        return True
    if not order.company_id:
        return False
    return order.company_id in order_accessible_company_uuids(user)


def can_shopify_edit_order(user: dict[str, Any], order: ShopifyOrder) -> bool:
    """Edición vía Order Edit API (no exige PENDING; sí exige mismo alcance que lectura)."""
    if user.get("role") not in INTERVENTION_ROLES:
        return False
    return can_read_order(user, order)


def can_push_shopify_edits(user: dict[str, Any], order: ShopifyOrder) -> bool:
    """Cantidades / envío vía Order Edit API (no aplica KPI_VISUALIZERS)."""
    if user.get("role") not in INTERVENTION_ROLES:
        return False
    return can_read_order(user, order)


def get_order_by_id(
    session: Session, order_id: str
) -> ShopifyOrder | None:
    try:
        uid = uuid.UUID(order_id)
    except ValueError:
        return None
    return session.get(ShopifyOrder, uid)
