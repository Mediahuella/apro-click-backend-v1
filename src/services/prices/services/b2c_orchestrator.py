"""Pipeline de carga de precios B2C/WEB (worker SQS).

``process_b2c_upload(upload_id, deadline_monotonic=None)``:

1. Descarga el Excel de S3 y lo parsea (reusa ``parse_price_list_excel``).
2. Arma ``{sku: precio}`` desde la columna ``price_b2c_iva`` (B2C/WEB).
3. Resuelve credenciales de la tienda B2C (env vars) y trae sus variantes.
4. Calcula el diff (match exacto + normalizado; omite las ya correctas).
5. Aplica ``productVariantsBulkUpdate`` por producto hasta agotar el diff o el
   *deadline*. Si quedan productos pendientes, **re-encola** el mismo upload
   (status ``PROCESSING``) y la próxima invocación continúa — es idempotente.

Status final: ``COMPLETED`` (sin pendientes ni errores), ``PARTIAL`` (errores),
``FAILED`` (sin precios en el Excel o ningún SKU coincide con la tienda).
"""
from __future__ import annotations

import sys
import uuid as uuid_mod
from pathlib import Path
from typing import Any

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

from database.engine import get_session  # noqa: E402
from database.models.price_list import B2cPriceUpload  # noqa: E402

from services.b2c_config import resolve_b2c_shop_and_token  # noqa: E402
from services.b2c_upload_service import enqueue_b2c_upload  # noqa: E402
from services.excel_parser import parse_price_list_excel  # noqa: E402
from services.s3_storage import get_excel  # noqa: E402
from services.shopify_b2c import (  # noqa: E402
    apply_b2c_prices,
    compute_b2c_diff,
    fetch_store_variants,
)

logger = Logger()

#: Máximo de SKU faltantes a guardar como muestra en la BD.
_MISSING_SAMPLE_MAX = 50


def _build_precios(items: list[Any]) -> dict[str, int]:
    """``{sap_code: precio_clp_entero}`` desde la columna B2C/WEB."""
    precios: dict[str, int] = {}
    for it in items:
        price = getattr(it, "price_b2c_iva", None)
        if price is None:
            continue
        try:
            value = int(round(float(price)))
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        precios[it.sap_code] = value
    return precios


def _set_failed(uid: uuid_mod.UUID, message: str) -> None:
    with get_session() as session:
        row = session.get(B2cPriceUpload, uid)
        if row is not None:
            row.status = "FAILED"
            row.error_message = message[:2000]
            session.commit()


def process_b2c_upload(
    upload_id: uuid_mod.UUID,
    *,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    """Procesa (o continúa) un upload de precios B2C. Devuelve un resumen."""
    with get_session() as session:
        row = session.get(B2cPriceUpload, upload_id)
        if row is None:
            raise LookupError(f"B2cPriceUpload '{upload_id}' no encontrado")
        bucket, key = row.s3_bucket, row.s3_key
        prev_updated = row.variants_updated or 0
        row.status = "PROCESSING"
        row.error_message = None
        session.commit()

    if not bucket or not key:
        _set_failed(upload_id, "El upload no tiene archivo en S3")
        raise ValueError("El upload no tiene bucket/key en S3")

    # 1-2. Descargar y parsear → precios B2C.
    excel_bytes = get_excel(bucket, key)
    parse_result = parse_price_list_excel(excel_bytes)
    precios = _build_precios(parse_result.items)
    if not precios:
        _set_failed(
            upload_id,
            "El Excel no tiene precios en la columna 'B2C/WEB (iva incluido)'",
        )
        raise ValueError("Excel sin precios B2C/WEB válidos")

    # 3. Credenciales + variantes de la tienda B2C.
    shop, token, api_version = resolve_b2c_shop_and_token()
    variants = fetch_store_variants(shop, token, api_version)

    # 4. Diff.
    diff = compute_b2c_diff(variants, precios)
    if diff.matched == 0:
        _set_failed(
            upload_id,
            "Ningún SKU del Excel coincide con variantes de la tienda B2C",
        )
        raise ValueError("Sin coincidencias de SKU en la tienda B2C")

    # 5. Aplicar con deadline.
    apply_res = apply_b2c_prices(
        shop,
        token,
        api_version,
        diff.por_producto,
        deadline_monotonic=deadline_monotonic,
    )

    pending = apply_res.products_remaining > 0
    total_updated = prev_updated + apply_res.updated
    missing_sample = ",".join(diff.missing_skus[:_MISSING_SAMPLE_MAX]) or None

    if pending:
        final_status = "PROCESSING"
    elif apply_res.errors > 0:
        final_status = "PARTIAL"
    else:
        final_status = "COMPLETED"

    with get_session() as session:
        row = session.get(B2cPriceUpload, upload_id)
        if row is not None:
            row.shop_domain = shop
            row.parsed_items = len(precios)
            row.variants_matched = diff.matched
            row.variants_already_ok = diff.already_ok
            row.variants_missing = len(diff.missing_skus)
            row.missing_skus_sample = missing_sample
            row.variants_updated = total_updated
            row.status = final_status
            if apply_res.errors > 0 and not pending:
                row.error_message = (
                    f"{apply_res.errors} variantes con error al actualizar"
                )
            session.commit()

    # Re-encolar continuación si quedó trabajo pendiente por el deadline.
    if pending:
        enqueue_b2c_upload(upload_id)

    summary = {
        "upload_id": str(upload_id),
        "status": final_status,
        "parsed_items": len(precios),
        "matched": diff.matched,
        "already_ok": diff.already_ok,
        "updated_this_run": apply_res.updated,
        "updated_total": total_updated,
        "errors": apply_res.errors,
        "missing": len(diff.missing_skus),
        "products_remaining": apply_res.products_remaining,
    }
    logger.info("B2C price upload procesado", extra=summary)
    return summary
