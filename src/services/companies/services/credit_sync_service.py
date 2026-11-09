"""Sincroniza el crédito de las empresas del CRM contra el DMS de APRO.

APRO es la **fuente de verdad** del crédito: si el DMS dice que la empresa
tiene un crédito vigente, ``Company.payment_type`` pasa a ``CREDIT``; si dice
que no, vuelve a ``DIRECT``. El campo del panel queda informativo — un cambio
manual lo revierte la próxima corrida.

Se dispara desde cuatro lugares:

- ``handlers/credit_sync.py`` — Lambda diaria (EventBridge) que recorre todas
  las empresas con RUT de facturación.
- ``POST /api/v1/companies/webhooks/apro-ia/estado-credito`` — APRO avisa que
  el crédito de una empresa cambió (``aplicar_webhook_credito``), para que el
  checkout no espere a la corrida del día siguiente.
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
import unicodedata
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

from apro.credit_client import (  # noqa: E402
    ENV_PROD,
    _ruts_forzados,
    consultar_credito,
    esta_configurado,
    normalizar_rut,
)
from database.engine import get_session  # noqa: E402
from database.models.client import Client  # noqa: E402
from database.models.company import Company  # noqa: E402
from notifications.sqs_publisher import encolar_credito_aprobado  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from utils.shopify_catalog_sync import _resolve_shop_and_token  # noqa: E402
from utils.shopify_credit_sync import sync_company_credit_metafield  # noqa: E402

logger = Logger()

PAYMENT_TYPE_CREDIT = "CREDIT"
PAYMENT_TYPE_DIRECT = "DIRECT"


def _contactos_para_aviso(session: Session, company: Company) -> list[dict]:
    """Contactos con email de la empresa, el principal primero y sin repetir."""
    filas = session.execute(
        select(Client.name, Client.email)
        .where(Client.company_id == company.id, Client.email.is_not(None))
        .order_by(Client.is_main_contact.desc(), Client.created_at)
    ).all()
    vistos: set[str] = set()
    salida: list[dict] = []
    for nombre, email in filas:
        email = (email or "").strip().lower()
        if not email or email in vistos:
            continue
        vistos.add(email)
        salida.append(
            {"contact_email": email, "contact_name": nombre, "company_name": company.name}
        )
    return salida


def _encolar_avisos_credito_aprobado(avisos: list[dict]) -> None:
    """Encola el correo «crédito aprobado». Se llama después del commit.

    No lanza: el correo es un aviso, el crédito ya quedó aplicado en la BD y en
    Shopify aunque la cola falle.
    """
    for aviso in avisos:
        try:
            encolar_credito_aprobado(**aviso)
        except Exception:
            logger.exception(
                "No se pudo encolar el aviso de crédito aprobado",
                extra={"empresa": aviso.get("company_name")},
            )
    if avisos:
        logger.info(
            "Avisos de crédito aprobado encolados",
            extra={"destinatarios": len(avisos)},
        )


def payment_type_para(tiene_credito: bool) -> str:
    return PAYMENT_TYPE_CREDIT if tiene_credito else PAYMENT_TYPE_DIRECT


def resolver_payment_type(rut: str, *, env: str = ENV_PROD) -> str | None:
    """Consulta el DMS y devuelve el ``payment_type`` que corresponde.

    ``None`` si no se pudo determinar (sin configuración, DMS caído): el
    llamador debe dejar la empresa como está.
    """
    credito = consultar_credito(rut, env=env)
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
    ruts: list[str], *, env: str = ENV_PROD, max_workers: int = 8
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
        futuros = {
            pool.submit(resolver_payment_type, rut, env=env): rut for rut in ruts
        }
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
    avisos: list[dict] = []
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
            if destino == PAYMENT_TYPE_CREDIT:
                avisos.extend(_contactos_para_aviso(session, company))
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
    _encolar_avisos_credito_aprobado(avisos)

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


# ---------------------------------------------------------------------------
# Webhook de APRO: cambio de estado del crédito de una empresa
# ---------------------------------------------------------------------------
# Estados de crédito que informa APRO en ``status``. Se comparan normalizados
# (``_normalizar_status``): sin acentos, en minúsculas y con espacios simples.
_STATUS_CON_CREDITO = frozenset({"aprobada continental", "aprobada orsan"})
_STATUS_SIN_CREDITO = frozenset({"anulada", "rechazado apro", "credito rechazado"})


def _normalizar_status(valor: str) -> str:
    sin_acentos = unicodedata.normalize("NFKD", valor).encode("ascii", "ignore").decode()
    return " ".join(sin_acentos.lower().split())


def _tiene_credito_del_status(valor) -> bool:
    """``status`` del webhook → bool.

    Con crédito: ``Aprobada Continental``, ``Aprobada Orsan``. Sin crédito:
    ``Anulada``, ``Rechazado Apro``, ``Crédito Rechazado``. También acepta un
    booleano. Cualquier otro valor es un payload inválido: adivinar aquí
    encendería o apagaría un medio de pago por un estado que no conocemos.
    """
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str):
        estado = _normalizar_status(valor)
        if estado in _STATUS_CON_CREDITO:
            return True
        if estado in _STATUS_SIN_CREDITO:
            return False
    raise ValueError(f"status es obligatorio y debe ser un estado de crédito conocido: {valor!r}")


def _rut_normalizado_sql():
    """``billing_rut`` normalizado en SQL, igual que ``normalizar_rut``."""
    expr = func.upper(func.coalesce(Company.billing_rut, ""))
    for separador in (".", "-", " "):
        expr = func.replace(expr, separador, "")
    return expr


def aplicar_webhook_credito(body: dict) -> dict:
    """Aplica el estado de crédito que informa APRO para un RUT.

    Payload::

        {"rut": "76486379-8", "status": "Aprobada Continental"}

    ``status`` admite un booleano o un estado conocido (ver
    ``_tiene_credito_del_status``). Actualiza
    ``payment_type`` de **todas** las empresas del CRM con ese RUT de
    facturación (normalmente una) y empuja el metafield ``aproclick.credito`` a
    Shopify. Es idempotente y se puede reintentar: el metafield se reescribe
    siempre, así un fallo de Shopify en el intento anterior queda reparado.

    Un RUT que no está en el CRM no es un error del emisor (APRO tiene clientes
    que no compran por la tienda): se responde ``ignored`` para que no
    reintente. Los RUT de ``APRO_CREDIT_FORCE_RUTS`` conservan el crédito
    aunque el webhook diga lo contrario, igual que en la corrida diaria.

    Lanza ``ValueError`` si el payload es inválido. Abre su propia sesión.
    """
    rut = normalizar_rut(str(body.get("rut") or ""))
    if not rut:
        raise ValueError("rut es obligatorio")
    status = body.get("status")
    tiene_credito = _tiene_credito_del_status(status)

    forzado = rut in _ruts_forzados()
    destino = payment_type_para(tiene_credito or forzado)

    # La conexión a Shopify se resuelve ANTES de abrir la sesión: el pool de la
    # Lambda tiene una sola conexión (``pool_size=1``) y
    # ``_resolve_shop_and_token`` abre su propia sesión. Llamarlo con esta
    # abierta lo deja esperando una conexión que nunca se libera, hasta el
    # timeout de la Lambda y sin log.
    try:
        shop_domain, access_token = _resolve_shop_and_token()
    except LookupError:
        logger.warning("Sin instalación Shopify activa: se actualiza solo la BD")
        shop_domain, access_token = "", ""

    empresas_resultado: list[dict] = []
    avisos: list[dict] = []
    with get_session() as session:
        empresas = list(
            session.scalars(
                select(Company).where(
                    _rut_normalizado_sql() == rut,
                    Company.is_system.is_(False),
                )
            ).all()
        )
        if not empresas:
            logger.warning(
                "Webhook de crédito para un RUT que no está en el CRM",
                extra={"rut": rut, "status": status, "tiene_credito": tiene_credito},
            )
            return {"received": True, "ignored": True, "matched": False}

        for company in empresas:
            antes = (company.payment_type or "").strip().upper()
            company.payment_type = destino
            # El metafield se escribe aunque la BD ya esté al día: si un intento
            # anterior dejó la BD actualizada pero Shopify falló, el reintento
            # del webhook lo repara (``metafieldsSet`` es idempotente).
            metafield_ok = sync_company_credit_metafield(
                company.shopify_company_id,
                destino,
                shop_domain=shop_domain,
                access_token=access_token,
            )
            # Solo en la transición: un reenvío de «aprobada» no vuelve a avisar.
            if destino == PAYMENT_TYPE_CREDIT and antes != PAYMENT_TYPE_CREDIT:
                avisos.extend(_contactos_para_aviso(session, company))
            empresas_resultado.append(
                {
                    "company_id": str(company.id),
                    "name": company.name,
                    "antes": antes,
                    "despues": destino,
                    "cambio": antes != destino,
                    "metafield_sincronizado": metafield_ok,
                }
            )
        session.commit()
    _encolar_avisos_credito_aprobado(avisos)

    logger.info(
        "Webhook de crédito aplicado",
        extra={
            "rut": rut,
            "status": status,
            "tiene_credito": tiene_credito,
            "forzado": forzado,
            "empresas": len(empresas),
            "cambios": sum(1 for e in empresas_resultado if e["cambio"]),
        },
    )
    return {
        "received": True,
        "ignored": False,
        "matched": True,
        "rut": rut,
        "payment_type": destino,
        "empresas": empresas_resultado,
    }
