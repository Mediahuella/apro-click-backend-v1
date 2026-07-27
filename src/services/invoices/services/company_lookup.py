"""Resolución de RUT de facturación a partir del shopify_company_id.

El bloque storefront solo conoce el GID de Shopify de la sesión B2B logueada
(``customer.current_company.id``) — nunca el RUT ni el UUID interno de
``Company``. Mismo principio que ``companies/handlers/companies.py``.
"""
from __future__ import annotations

import sys
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
    if path.exists() and (path / "database").exists():
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        break

from database.engine import get_session
from database.models.company import Company
from sqlalchemy import select


class CompanyNotFoundError(LookupError):
    pass


class MissingBillingRutError(ValueError):
    pass


def get_billing_rut(shopify_company_id: str) -> str:
    """Devuelve el ``billing_rut`` de la empresa asociada a ``shopify_company_id``.

    Lanza ``CompanyNotFoundError`` si la empresa no existe y
    ``MissingBillingRutError`` si existe pero no tiene RUT de facturación
    configurado.
    """
    shopify_company_id = str(shopify_company_id or "").strip()
    with get_session() as session:
        company = session.scalar(
            select(Company).where(Company.shopify_company_id == shopify_company_id)
        )
        if not company:
            raise CompanyNotFoundError("Empresa no encontrada")
        rut = (company.billing_rut or "").strip()
        if not rut:
            raise MissingBillingRutError(
                "La empresa no tiene RUT de facturación configurado"
            )
        return rut
