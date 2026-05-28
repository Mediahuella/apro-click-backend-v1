"""Job programado (EventBridge) — reconciliación horaria de ``aproclick.stock``.

Recorre todas las instalaciones Shopify activas y, por cada una, reconcilia el
metafield ``aproclick.stock`` de todas las variantes con un Bulk Export. Actúa
como safety-net ante eventos ``inventory_levels/update`` que el webhook no
alcance a procesar (perdidos, no entregados o con la tienda sin token al
momento del evento).

Se invoca por un ``schedule: rate(1 hour)`` definido en ``serverless.yml``.
"""
from __future__ import annotations

import sys
from pathlib import Path

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext

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

from sqlalchemy import select  # noqa: E402

from database.engine import get_session  # noqa: E402
from database.models.shopify import ShopifyAppInstallation  # noqa: E402
from services.shopify_stock_reconcile import reconcile_shop_stock  # noqa: E402

logger = Logger()
tracer = Tracer()


def _active_installations() -> list[tuple[str, str]]:
    with get_session() as session:
        rows = session.scalars(
            select(ShopifyAppInstallation).where(
                ShopifyAppInstallation.uninstalled_at.is_(None),
            )
        ).all()
    out: list[tuple[str, str]] = []
    for row in rows:
        token = (row.shopify_access_token or "").strip()
        if token:
            out.append((row.shop_domain, token))
    return out


@tracer.capture_lambda_handler
def lambda_handler(event: dict, context: LambdaContext) -> dict:
    installations = _active_installations()
    if not installations:
        logger.info("stock reconcile — sin instalaciones activas")
        return {"shops": 0, "results": []}

    results: list[dict] = []
    for shop_domain, token in installations:
        try:
            result = reconcile_shop_stock(shop_domain, token)
        except Exception:
            logger.exception(
                "stock reconcile — error reconciliando tienda",
                extra={"shop_domain": shop_domain},
            )
            results.append({"shop_domain": shop_domain, "error": True})
            continue

        logger.info(
            "stock reconcile ok",
            extra={
                "shop_domain": result.shop_domain,
                "variants": result.variants,
                "written": result.written,
                "errors": result.errors,
                "skipped_reason": result.skipped_reason,
            },
        )
        results.append(
            {
                "shop_domain": result.shop_domain,
                "variants": result.variants,
                "written": result.written,
                "errors": result.errors,
                "skipped_reason": result.skipped_reason,
            }
        )

    return {"shops": len(installations), "results": results}
