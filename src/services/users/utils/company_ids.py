"""Normaliza IDs de empresas desde JSON (snake / camel) para `user_companies`."""
from __future__ import annotations

from typing import Any

# Orden: primera clave presente en el body gana
_BODY_KEYS: tuple[str, ...] = (
    "order_company_ids",
    "company_ids",
    "companyIds",
    "orderCompanyIds",
)


def coerce_to_company_id_list(raw: Any) -> list[str]:
    """Convierte string UUID, lista de str/uuid o vacío en lista de strings normalizados."""
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        return [s] if s else []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(
            "IDs de empresas deben ser un array de UUIDs o un único UUID en string"
        )
    out: list[str] = []
    for x in raw:
        if x is None:
            continue
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def company_ids_from_request_body(body: dict[str, Any] | None) -> list[str] | None:
    """
    None = ninguna de las claves soportadas en el body (no tocar asociaciones al actualizar).
    list (posible vacía) = reemplazar asociaciones con ese conjunto.
    """
    if not body:
        return None
    for k in _BODY_KEYS:
        if k in body:
            return coerce_to_company_id_list(body[k])
    return None


def company_ids_in_update(updates: dict[str, Any]) -> tuple[bool, list[str]]:
    """
    (True, lista) = el cliente envió una de las claves; hay que reemplazar `user_companies`.
    (False, []) = no envió ninguna; no tocar `user_companies`.
    """
    for k in _BODY_KEYS:
        if k in updates:
            return True, coerce_to_company_id_list(updates[k])
    return False, []

