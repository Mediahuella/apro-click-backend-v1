"""Lookup masivo SKU → ProductVariant GID en Shopify.

Estrategia: paginar ``productVariants`` con ``first: 250`` (máximo permitido)
hasta agotar la tienda y construir un dict ``{sku: variant_gid}`` en memoria.

Para una tienda con ~10-15k variantes son ~40-60 llamadas GraphQL (~30-60s).
La query trae sólo ``id`` y ``sku`` para minimizar el ``cost`` y evitar
throttling.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Iterable

from aws_lambda_powertools import Logger

service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

lambda_root = "/var/task"
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

from utils.shopify_graphql import graphql_call  # noqa: E402

logger = Logger()


_PRODUCT_VARIANTS_QUERY = """
query VariantsBySku($first: Int!, $after: String) {
  productVariants(first: $first, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      sku
    }
  }
}
"""

#: Tope de páginas como cinturón de seguridad. ~250 * 1000 = 250k variantes.
_MAX_PAGES = 1000

#: Espera entre páginas para mitigar el bucket de Shopify (1000 puntos).
_THROTTLE_SLEEP_SEC = 0.05


def fetch_all_sku_to_variant_id(
    shop_domain: str,
    access_token: str,
    *,
    page_size: int = 250,
) -> dict[str, str]:
    """Devuelve ``{sku: variant_gid}`` para todas las variantes con SKU no vacío.

    - Variantes sin SKU se ignoran.
    - Si el mismo SKU aparece en varias variantes (raro), gana la última.
    - Lanza ``ValueError`` desde ``graphql_call`` si Shopify falla.
    """
    if page_size < 1 or page_size > 250:
        raise ValueError("page_size debe estar entre 1 y 250 (límite Shopify)")

    out: dict[str, str] = {}
    after: str | None = None
    pages = 0
    total_seen = 0

    while pages < _MAX_PAGES:
        data = graphql_call(
            shop_domain,
            access_token,
            _PRODUCT_VARIANTS_QUERY,
            {"first": page_size, "after": after},
        )
        conn = data.get("productVariants") or {}
        nodes = conn.get("nodes") or []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            sku = (node.get("sku") or "").strip()
            vid = node.get("id")
            if not sku or not isinstance(vid, str):
                continue
            out[sku] = vid
        total_seen += len(nodes)
        page_info = conn.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        if not after:
            break
        pages += 1
        time.sleep(_THROTTLE_SLEEP_SEC)

    logger.info(
        "Shopify productVariants lookup",
        extra={
            "pages": pages + 1,
            "variants_seen": total_seen,
            "sku_to_variant_count": len(out),
        },
    )
    return out


def resolve_variants_for_skus(
    sku_to_variant: dict[str, str],
    skus: Iterable[str],
) -> tuple[dict[str, str], list[str]]:
    """Filtra ``sku_to_variant`` con sólo las SKUs deseadas.

    Devuelve ``(matched, missing)`` donde ``matched`` es el subset
    encontrado y ``missing`` la lista de SKUs sin variante.
    """
    matched: dict[str, str] = {}
    missing: list[str] = []
    for raw in skus:
        s = (raw or "").strip()
        if not s:
            continue
        vid = sku_to_variant.get(s)
        if vid:
            matched[s] = vid
        else:
            missing.append(s)
    return matched, missing
