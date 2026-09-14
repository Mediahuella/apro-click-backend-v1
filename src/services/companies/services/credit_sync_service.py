"""Sincroniza el crédito de las empresas del CRM contra el DMS de APRO.

APRO es la **fuente de verdad** del crédito: si el DMS dice que la empresa
tiene un crédito vigente, ``Company.payment_type`` pasa a ``CREDIT``; si dice
que no, vuelve a ``DIRECT``. El campo del panel queda informativo — un cambio
manual lo revierte la próxima corrida.

Se dispara desde tres lugares:

- ``handlers/credit_sync.py`` — Lambda diaria (EventBridge) que recorre todas
  las empresas con RUT de facturación.
- ``company-registration`` al aprobar una solicitud, para que el cliente nuevo
  nazca con el crédito que ya tenga en APRO.
- ``scripts/backfill_company_credit_from_dms.py``, para correrlo a mano.

Cada cambio se propaga a Shopify (metafield ``aproclick.credito``), que es lo
que lee la Function ``manejo-de-credito`` en el checkout.

**Indeterminado no es "sin crédito".** Si el DMS no responde, la empresa se
deja como está: apagar el crédito por un timeout dejaría a un cliente sin su
medio de pago, y encenderlo por error se lo daría a quien no corresponde.
"""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

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

from apro.credit_client import consultar_credito, esta_configurado  # noqa: E402
from database.engine import get_session  # noqa: E402
from database.models.company import Company  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from utils.shopify_catalog_sync import _resolve_shop_and_token  # noqa: E402
from utils.shopify_credit_sync import sync_company_credit_metafield  # noqa: E402

logger = Logger()

PAYMENT_TYPE_CREDIT = "CREDIT"
PAYMENT_TYPE_DIRECT = "DIRECT"


def payment_type_para(tiene_credito: bool) -> str:
    return PAYMENT_TYPE_CREDIT if tiene_credito else PAYMENT_TYPE_DIRECT


def resolver_payment_type(rut: str) -> str | None:
    """Consulta el DMS y devuelve el ``payment_type`` que corresponde.

    ``None`` si no se pudo determinar (sin configuración, DMS caído): el
    llamador debe dejar la empresa como está.
    """
    credito = consultar_credito(rut)
    if credito is None:
        return None
    return payment_type_para(bool(credito.get("tiene_credito")))


def sincronizar_empresa(
    session: Session,
    company: Company,
    *,
    shop_domain: str = "",
    access_token: str = "",
) -> dict:
    """Sincroniza una empresa. Devuelve ``{rut, antes, despues, cambio}``.

    No hace commit: lo decide quien abre la sesión.
    """
    rut = (company.billing_rut or "").strip()
    antes = (company.payment_type or "").strip().upper()
    resultado = {
        "company_id": str(company.id),
        "name": company.name,
        "rut": rut,
        "antes": antes,
        "despues": antes,
        "cambio": False,
        "indeterminado": False,
    }
    if not rut:
        # Sin RUT no hay a quién preguntarle: las empresas del CRM sin RUT de
        # facturación no participan de la sincronización.
        return resultado

    destino = resolver_payment_type(rut)
    if destino is None:
        resultado["indeterminado"] = True
        return resultado

    resultado["despues"] = destino
    if destino == antes:
        return resultado

    company.payment_type = destino
    resultado["cambio"] = True
    sync_company_credit_metafield(
        company.shopify_company_id,
        destino,
        shop_domain=shop_domain,
        access_token=access_token,
    )
    logger.info(
        "Crédito sincronizado desde APRO",
        extra={"rut": rut, "antes": antes, "despues": destino, "empresa": company.name},
    )
    return resultado


def consultar_en_paralelo(
    ruts: list[str], *, max_workers: int = 8
) -> dict[str, str | None]:
    """``{rut: payment_type|None}`` consultando el DMS en paralelo.

    Secuencial no sirve: el CRM tiene casi mil empresas y, a un request por
    empresa, una pasada no cabe en los 15 minutos de la Lambda. Ocho hilos
    dejan la corrida en un par de minutos sin castigar al DMS.
    """
    destinos: dict[str, str | None] = {}
    if not ruts:
        return destinos
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futuros = {pool.submit(resolver_payment_type, rut): rut for rut in ruts}
        for futuro in as_completed(futuros):
            rut = futuros[futuro]
            try:
                destinos[rut] = futuro.result()
            except Exception:  # noqa: BLE001 — el cliente ya no lanza; por si acaso
                logger.warning("Consulta de crédito fallida", extra={"rut": rut})
                destinos[rut] = None
    return destinos


def sincronizar_todas(*, limite: int | None = None) -> dict:
    """Recorre las empresas con RUT y las sincroniza. Abre su propia sesión.

    Dos fases: primero consulta al DMS en paralelo (solo lectura HTTP) y
    después aplica los cambios en la BD y en Shopify. Así el recorrido no queda
    a merced de la latencia del DMS empresa por empresa.
    """
    if not esta_configurado():
        logger.warning(
            "Sincronización de crédito omitida: falta APRO_CREDIT_BASE_URL "
            "o APRO_CREDIT_BEARER_TOKEN"
        )
        return {"configurado": False, "revisadas": 0, "cambios": [], "indeterminadas": 0}

    # Se resuelve una sola vez la conexión a Shopify: el recorrido puede tocar
    # cientos de empresas y `sync_company_credit_metafield` la pediría en cada
    # una.
    try:
        shop_domain, access_token = _resolve_shop_and_token()
    except LookupError:
        logger.warning("Sin instalación Shopify activa: se sincroniza solo la BD")
        shop_domain, access_token = "", ""

    cambios: list[dict] = []
    indeterminadas = 0
    revisadas = 0
    with get_session() as session:
        q = select(Company).where(
            Company.billing_rut.is_not(None),
            Company.is_system.is_(False),
        ).order_by(Company.name)
        if limite:
            q = q.limit(limite)
        empresas = list(session.scalars(q).all())

        destinos = consultar_en_paralelo(
            sorted({(c.billing_rut or "").strip() for c in empresas if c.billing_rut})
        )

        for company in empresas:
            revisadas += 1
            destino = destinos.get((company.billing_rut or "").strip())
            antes = (company.payment_type or "").strip().upper()
            if destino is None:
                indeterminadas += 1
                continue
            if destino == antes:
                continue
            company.payment_type = destino
            sync_company_credit_metafield(
                company.shopify_company_id,
                destino,
                shop_domain=shop_domain,
                access_token=access_token,
            )
            cambios.append(
                {
                    "company_id": str(company.id),
                    "name": company.name,
                    "rut": company.billing_rut,
                    "antes": antes,
                    "despues": destino,
                }
            )
            logger.info(
                "Crédito sincronizado desde APRO",
                extra={"rut": company.billing_rut, "antes": antes, "despues": destino},
            )
        session.commit()

    logger.info(
        "Sincronización de crédito terminada",
        extra={
            "revisadas": revisadas,
            "cambios": len(cambios),
            "indeterminadas": indeterminadas,
        },
    )
    return {
        "configurado": True,
        "revisadas": revisadas,
        "cambios": cambios,
        "indeterminadas": indeterminadas,
    }
