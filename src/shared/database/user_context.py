"""Enriquecimiento de dict de usuario: empresas asignadas para pedidos y chat."""
from __future__ import annotations

import uuid as uuid_mod
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models.company import Company
from database.models.user import User
from database.models.user_company import UserCompany

# Roles de tenant: el ``company_id`` por defecto suele ser la empresa plataforma
# (``companies.is_system``) al crear usuarios; no debe mezclarse con el alcance
# de pedidos/chat de comercios.
_TENANT_SCOPE_ROLES = frozenset({"SALES", "KPI_VISUALIZERS"})


def _platform_company_id(session: Session) -> uuid_mod.UUID | None:
    key = "_mh_platform_company_id"
    if key in session.info:
        return session.info[key]
    pid = session.scalars(
        select(Company.id)
        .where(Company.is_system.is_(True))
        .order_by(Company.id.asc())
        .limit(1)
    ).first()
    session.info[key] = pid
    return pid


def attach_order_company_ids(session: Session, user_row: User, d: dict[str, Any]) -> None:
    """
    Añade ``order_company_ids: list[str]`` (UUIDs de ``companies.id``).

    Fuente: filas en ``user_companies`` (orden por ``company_id``). Si el usuario
    tiene ``users.company_id`` y no estaba en esa lista (p. ej. datos previos a la
    tabla M2M), se añade al final para no dejar el alcance vacío.

    Para ``SALES`` y ``KPI_VISUALIZERS``, la empresa plataforma (``is_system``) se
    excluye del alcance: no hay conversaciones ni pedidos de cliente bajo esa fila.
    """
    ids = session.scalars(
        select(UserCompany.company_id)
        .where(UserCompany.user_id == user_row.id)
        .order_by(UserCompany.company_id.asc())
    ).all()
    seen: set[str] = set()
    out: list[str] = []
    for x in ids:
        s = str(x)
        if s not in seen:
            seen.add(s)
            out.append(s)
    if user_row.company_id is not None:
        s = str(user_row.company_id)
        if s not in seen:
            out.append(s)
    if user_row.role in _TENANT_SCOPE_ROLES:
        plat = _platform_company_id(session)
        if plat is not None:
            ps = str(plat)
            out = [x for x in out if x != ps]
    d["order_company_ids"] = out
