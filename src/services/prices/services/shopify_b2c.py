"""Lógica Shopify para la carga de precios B2C/WEB.

Escribe el **precio por defecto** de las variantes (``price`` y
``compareAtPrice`` iguales al valor de la columna "B2C/WEB (iva incluido)") en
la tienda B2C. Tres piezas:

1. ``fetch_store_variants`` — pagina ``productVariants`` trayendo
   ``sku, price, compareAtPrice`` y el ``product.id`` (para agrupar).
2. ``compute_b2c_diff`` — calcula qué variantes hay que actualizar (match
   exacto + normalizado tolerante a ceros a la izquierda; omite las ya
   correctas) y qué SKU del Excel no existen en la tienda.
3. ``apply_b2c_prices`` — aplica ``productVariantsBulkUpdate`` por producto,
   con rate-limit, reintentos ante throttling y un *deadline* opcional para no
   exceder el timeout del Lambda (el worker re-encola la continuación).

El diseño es idempotente: los precios se fijan en valor absoluto y el diff
omite las ya correctas, así que reprocesar es seguro y barato.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger

service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

lambda_root = "/var/task"
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

from utils.shopify_graphql import format_user_errors, graphql_call  # noqa: E402

logger = Logger()

#: Tope de páginas como cinturón de seguridad (~250 * 1000 = 250k variantes).
_MAX_PAGES = 1000
#: Espera entre páginas para mitigar el bucket de Shopify (1000 puntos).
_THROTTLE_SLEEP_SEC = 0.05

_STORE_VARIANTS_QUERY = """
query B2cVariants($first: Int!, $after: String) {
  productVariants(first: $first, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      sku
      price
      compareAtPrice
      product {
        id
      }
    }
  }
}
"""

_VARIANTS_UPDATE_MUTATION = """
mutation B2cUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    userErrors {
      field
      message
      code
    }
    productVariants {
      id
    }
  }
}
"""


@dataclass(frozen=True)
class StoreVariant:
    variant_gid: str
    product_gid: str
    sku: str
    price: Any
    compare_at_price: Any


@dataclass
class B2cDiff:
    #: ``{product_gid: [{"id","price","compareAtPrice"}, ...]}`` a actualizar.
    por_producto: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    matched: int = 0
    already_ok: int = 0
    missing_skus: list[str] = field(default_factory=list)

    @property
    def to_update(self) -> int:
        return sum(len(v) for v in self.por_producto.values())


def normalizar_sku(sku: str) -> str:
    """Clave de comparación tolerante a ceros a la izquierda y formato.

    Mantiene solo alfanuméricos, mayúsculas y quita ceros iniciales. El Excel no
    tiene colisiones bajo esta normalización, así que sirve para rescatar SKU
    que en la tienda están con ceros a la izquierda (p. ej. ``00300000270000``
    en la tienda vs ``300000270000`` en el Excel).
    """
    return "".join(ch for ch in sku if ch.isalnum()).lstrip("0").upper()


def _price_eq(actual: Any, objetivo: int) -> bool:
    """Compara precio Shopify (string decimal o None) contra entero objetivo."""
    if actual is None:
        return False
    try:
        return int(round(float(actual))) == objetivo
    except (TypeError, ValueError):
        return False


def fetch_store_variants(
    shop_domain: str,
    access_token: str,
    api_version: str,
    *,
    page_size: int = 250,
) -> list[StoreVariant]:
    """Pagina todas las variantes con SKU no vacío de la tienda B2C."""
    if page_size < 1 or page_size > 250:
        raise ValueError("page_size debe estar entre 1 y 250 (límite Shopify)")

    out: list[StoreVariant] = []
    after: str | None = None
    pages = 0

    while pages < _MAX_PAGES:
        data = graphql_call(
            shop_domain,
            access_token,
            _STORE_VARIANTS_QUERY,
            {"first": page_size, "after": after},
            api_version=api_version,
        )
        conn = data.get("productVariants") or {}
        for node in conn.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            sku = (node.get("sku") or "").strip()
            vid = node.get("id")
            product = node.get("product") or {}
            pid = product.get("id") if isinstance(product, dict) else None
            if not sku or not isinstance(vid, str) or not isinstance(pid, str):
                continue
            out.append(
                StoreVariant(
                    variant_gid=vid,
                    product_gid=pid,
                    sku=sku,
                    price=node.get("price"),
                    compare_at_price=node.get("compareAtPrice"),
                )
            )
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break
        pages += 1
        time.sleep(_THROTTLE_SLEEP_SEC)

    logger.info(
        "Shopify B2C productVariants fetch",
        extra={"pages": pages + 1, "variants_with_sku": len(out)},
    )
    return out


def compute_b2c_diff(
    variants: list[StoreVariant],
    precios: dict[str, int],
) -> B2cDiff:
    """Calcula el diff entre la tienda y los precios del Excel.

    - ``precios``: ``{sku_excel: precio_clp_entero}`` (columna B2C/WEB).
    - Match exacto y, como fallback, normalizado (``normalizar_sku``).
    - Objetivo por variante: ``price == compareAtPrice == precio``.
    - Omite las variantes ya correctas; agrupa el resto por producto.
    """
    precios_norm: dict[str, tuple[str, int]] = {
        normalizar_sku(k): (k, v) for k, v in precios.items()
    }
    diff = B2cDiff()
    skus_en_tienda: set[str] = set()

    for v in variants:
        sku_excel = v.sku
        objetivo = precios.get(v.sku)
        if objetivo is None:
            par = precios_norm.get(normalizar_sku(v.sku))
            if par is not None:
                sku_excel, objetivo = par
        if objetivo is None:
            continue
        skus_en_tienda.add(sku_excel)
        diff.matched += 1
        if _price_eq(v.price, objetivo) and _price_eq(v.compare_at_price, objetivo):
            diff.already_ok += 1
            continue
        diff.por_producto.setdefault(v.product_gid, []).append(
            {
                "id": v.variant_gid,
                "price": str(objetivo),
                "compareAtPrice": str(objetivo),
            }
        )

    diff.missing_skus = sorted(set(precios) - skus_en_tienda)
    return diff


@dataclass
class ApplyResult:
    updated: int = 0
    errors: int = 0
    products_done: int = 0
    products_remaining: int = 0


def apply_b2c_prices(
    shop_domain: str,
    access_token: str,
    api_version: str,
    por_producto: dict[str, list[dict[str, str]]],
    *,
    rate_ms: int = 250,
    deadline_monotonic: float | None = None,
) -> ApplyResult:
    """Aplica ``productVariantsBulkUpdate`` por producto.

    Si ``deadline_monotonic`` se alcanza, detiene el proceso y reporta los
    productos pendientes en ``products_remaining`` (el worker re-encola).
    """
    result = ApplyResult()
    items = list(por_producto.items())

    for idx, (product_gid, variants) in enumerate(items):
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            result.products_remaining = len(items) - idx
            logger.info(
                "B2C apply: deadline alcanzado, re-encolando continuación",
                extra={"products_done": result.products_done},
            )
            break

        for attempt in range(9):
            try:
                data = graphql_call(
                    shop_domain,
                    access_token,
                    _VARIANTS_UPDATE_MUTATION,
                    {"productId": product_gid, "variants": variants},
                    api_version=api_version,
                )
                ue = (data.get("productVariantsBulkUpdate") or {}).get(
                    "userErrors"
                ) or []
                blob = format_user_errors(ue).upper() if ue else ""
                if "THROTTLE" in blob and attempt < 8:
                    time.sleep(max(rate_ms * (attempt + 1) * 2, 1500) / 1000.0)
                    continue
                if ue:
                    logger.warning(
                        "productVariantsBulkUpdate userErrors",
                        extra={"product_gid": product_gid, "errors": ue},
                    )
                    result.errors += len(variants)
                else:
                    result.updated += len(variants)
                break
            except ValueError as e:
                if attempt >= 8:
                    result.errors += len(variants)
                    logger.exception(
                        "Error aplicando precios B2C al producto",
                        extra={"product_gid": product_gid, "detail": str(e)},
                    )
                    break
                time.sleep(max(rate_ms * (attempt + 1) * 2, 1500) / 1000.0)

        result.products_done += 1
        time.sleep(max(0, rate_ms / 1000.0))

    return result
