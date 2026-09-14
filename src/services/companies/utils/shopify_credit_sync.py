"""Sincronización de ``Company.payment_type`` → metafield de crédito en Shopify.

La Shopify Function ``manejo-de-credito`` decide en el checkout si muestra el
método de pago con crédito. Una Function **no puede llamar a nuestra API**: solo
lee lo que venga en su input query. El puente es un metafield en la Company B2B
de Shopify:

    namespace ``aproclick`` · key ``credito`` · type ``boolean``

El CRM es la fuente de verdad (``companies.payment_type``): ``CREDIT`` → ``true``,
``DIRECT`` → ``false``. Cada vez que cambia el tipo de pago de una empresa, este
módulo empuja el valor a Shopify.

No bloquea: si Shopify falla, se loguea un warning y la BD queda actualizada
igual — el metafield se puede reponer con
``scripts/backfill_company_credit_metafield.py``. Preferimos eso a dejar al
usuario del panel sin poder guardar por un problema de la Admin API.

**Falla cerrada**: si el metafield no existe en Shopify, la Function trata a la
empresa como *sin* crédito. Un backfill pendiente nunca habilita crédito de más.
"""
from __future__ import annotations

import sys
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

from utils.shopify_catalog_sync import _resolve_shop_and_token  # noqa: E402
from utils.shopify_graphql import format_user_errors, graphql_call  # noqa: E402

logger = Logger()

#: Namespace/key del metafield que lee la Function. Cambiarlos obliga a
#: actualizar también ``extensions/manejo-de-credito/src/*.graphql`` en
#: ``aproclick-connector``.
CREDIT_METAFIELD_NAMESPACE = "aproclick"
CREDIT_METAFIELD_KEY = "credito"

_METAFIELDS_SET = """
mutation SetCompanyCredito($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) {
    metafields { id namespace key value }
    userErrors { field message code }
  }
}
"""


def _normalize_company_gid(value: str) -> str:
    """Acepta ``"3567190200"`` o el GID completo."""
    s = str(value or "").strip()
    if not s:
        return ""
    if s.startswith("gid://shopify/Company/"):
        return s
    return f"gid://shopify/Company/{s}"


def credit_metafield_value(payment_type: str | None) -> str:
    """``payment_type`` del CRM → valor del metafield boolean de Shopify."""
    return "true" if str(payment_type or "").strip().upper() == "CREDIT" else "false"


def sync_company_credit_metafield(
    shopify_company_id: str | None,
    payment_type: str | None,
    *,
    shop_domain: str = "",
    access_token: str = "",
) -> bool:
    """Escribe ``aproclick.credito`` en la Company de Shopify. No lanza.

    ``shop_domain``/``access_token`` se pueden pasar para reusar la conexión
    (el backfill recorre cientos de empresas y no tiene sentido que resuelva la
    instalación en cada una).

    Devuelve ``True`` si el metafield quedó escrito.
    """
    company_gid = _normalize_company_gid(shopify_company_id or "")
    if not company_gid:
        # Empresa del CRM que todavía no existe en Shopify B2B: no hay dónde
        # escribir. Al crearla (approve_request o el script de creación) se
        # sincroniza.
        return False

    value = credit_metafield_value(payment_type)

    try:
        if not (shop_domain and access_token):
            shop_domain, access_token = _resolve_shop_and_token()
    except LookupError as e:
        logger.warning(
            "sync_company_credit_metafield: no hay instalación Shopify activa — %s", e
        )
        return False

    variables = {
        "metafields": [
            {
                "ownerId": company_gid,
                "namespace": CREDIT_METAFIELD_NAMESPACE,
                "key": CREDIT_METAFIELD_KEY,
                "type": "boolean",
                "value": value,
            }
        ]
    }

    try:
        data = graphql_call(shop_domain, access_token, _METAFIELDS_SET, variables)
        errs = (data.get("metafieldsSet") or {}).get("userErrors") or []
        if errs:
            logger.warning(
                "sync_company_credit_metafield: metafieldsSet con userErrors",
                extra={
                    "company_gid": company_gid,
                    "errors": format_user_errors(errs),
                },
            )
            return False
    except Exception as e:  # noqa: BLE001 — nunca bloquea el guardado en BD
        logger.warning(
            "sync_company_credit_metafield: falló la llamada a Shopify — %s",
            e,
            extra={"company_gid": company_gid},
        )
        return False

    logger.info(
        "Metafield de crédito sincronizado",
        extra={"company_gid": company_gid, "credito": value},
    )
    return True
