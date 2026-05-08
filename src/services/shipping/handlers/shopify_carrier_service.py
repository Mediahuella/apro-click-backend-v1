"""Callback Shopify CarrierService — POST /api/v1/shipping/shopify/carrier-service.

Shopify llama este endpoint durante el checkout pidiendo cotizaciones de envío.
La spec oficial está en
https://shopify.dev/docs/api/admin-rest/latest/resources/carrierservice.

Notas críticas:

1. **Sin auth:** Shopify no envía HMAC para CarrierService. Validamos
   ``X-Shopify-Shop-Domain`` contra ``SHIPPING_ALLOWED_SHOP_DOMAINS`` (CSV)
   si está configurada (defensa en profundidad). Si la lista está vacía
   aceptamos cualquier shop (útil para staging).
2. **Timeouts:** Shopify da 10s en escenarios normales. Mantener el handler
   liviano: solo carga JSONs (cacheados con ``lru_cache``).
3. **Subunidades:** CLP no tiene subunidades. Shopify requiere multiplicar
   por 100 (10884 CLP → ``total_price: "1088400"``).
4. **Items sin dimensiones:** Shopify sólo manda ``grams`` por línea. El
   peso volumétrico requiere dimensiones por variante; en esta primera
   versión sumamos peso físico (kg = grams * qty / 1000) y el volumétrico
   queda en 0. Se puede ampliar leyendo metafields de variante en una
   próxima iteración.
5. **Sin tarifa para la localidad:** devolvemos ``{"rates": []}`` con 200
   para que el cliente vea otras opciones / pueda elegir retiro. Lanzar
   404 forzaría "backup rates" del checkout.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger, Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext

service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

lambda_root = "/var/task"
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

from services.shipping_quote import (  # noqa: E402
    LocalityNotFoundError,
    Package,
    RouteNotFoundError,
    WeightOutOfRangeError,
    get_iva_pct_default,
    get_origin_default,
    quote,
)

logger = Logger()
tracer = Tracer()


def _header(headers: dict | None, name: str) -> str | None:
    if not headers:
        return None
    target = name.lower()
    for k, v in headers.items():
        if str(k).lower() == target:
            return (v or "").strip() or None
    return None


def _allowed_shops() -> set[str]:
    raw = (os.environ.get("SHIPPING_ALLOWED_SHOP_DOMAINS") or "").strip()
    if not raw:
        return set()
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def _is_shop_allowed(shop_domain: str | None) -> bool:
    allowed = _allowed_shops()
    if not allowed:
        return True
    if not shop_domain:
        return False
    return shop_domain.strip().lower() in allowed


def _parse_body(event: dict) -> dict[str, Any]:
    raw = event.get("body")
    if raw is None:
        return {}
    if event.get("isBase64Encoded"):
        import base64

        if isinstance(raw, str):
            raw = base64.b64decode(raw).decode("utf-8")
        else:
            return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
    if isinstance(raw, dict):
        return raw
    return {}


def _default_package_cm() -> float:
    """Lado del cubo por defecto en cm (placeholder hasta tener dim por variante)."""
    raw = (os.environ.get("SHIPPING_DEFAULT_PKG_CM") or "10").strip()
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 10.0
    return max(0.0, v)


def _packages_from_items(items: list[dict[str, Any]]) -> list[Package]:
    """Genera un :class:`Package` **por unidad** vendida.

    Shopify no manda dimensiones por línea. Mientras tanto asumimos un cubo
    de ``SHIPPING_DEFAULT_PKG_CM`` (default 10 cm = 0,001 m³ → ~0,25 kg
    volumétrico por unidad) y un peso físico = ``grams / 1000`` por unidad.
    El día que tengamos dimensiones reales por variante (metafields
    ``aproclick.dim_*``), reemplazamos esta función.
    """
    side = _default_package_cm()
    packages: list[Package] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("requires_shipping") is False:
            continue
        try:
            grams = float(item.get("grams") or 0)
            qty = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0 or grams < 0:
            continue
        weight_per_unit_kg = grams / 1000.0
        for _ in range(qty):
            packages.append(
                Package(
                    weight_kg=weight_per_unit_kg,
                    height_cm=side,
                    length_cm=side,
                    width_cm=side,
                )
            )
    return packages


def _service_name() -> str:
    return (
        os.environ.get("SHIPPING_SERVICE_NAME") or "Apro Click — Carga consolidada"
    ).strip()


def _service_code() -> str:
    return (
        os.environ.get("SHIPPING_SERVICE_CODE") or "aproclick_consolidada"
    ).strip()


def _empty_response() -> dict[str, Any]:
    return {"statusCode": 200, "body": json.dumps({"rates": []})}


@tracer.capture_lambda_handler
def lambda_handler(event: dict, context: LambdaContext) -> dict:
    headers = event.get("headers") or {}
    shop_domain = _header(headers, "x-shopify-shop-domain")
    if not _is_shop_allowed(shop_domain):
        logger.warning("Carrier service: shop no permitido (%s)", shop_domain)
        return {"statusCode": 401, "body": json.dumps({"error": "shop not allowed"})}

    payload = _parse_body(event)
    rate = payload.get("rate") or {}
    if not isinstance(rate, dict):
        return _empty_response()

    destination = rate.get("destination") or {}
    items = rate.get("items") or []
    currency = (rate.get("currency") or "CLP").strip().upper()

    if currency != "CLP":
        # Por ahora solo cotizamos para Chile. Devolver vacío para no romper checkout.
        logger.info("Carrier service: currency %s no soportada", currency)
        return _empty_response()

    locality = ""
    for key in ("city", "province", "name"):
        v = destination.get(key) if isinstance(destination, dict) else None
        if v and isinstance(v, str) and v.strip():
            locality = v.strip()
            if key == "city":
                break

    if not locality:
        return _empty_response()

    packages = _packages_from_items(items if isinstance(items, list) else [])

    origin = get_origin_default()
    iva = get_iva_pct_default()

    try:
        result = quote(
            destination_locality=locality,
            packages=packages,
            origin=origin,
            iva_pct=iva,
        )
    except LocalityNotFoundError:
        logger.info("Carrier service: localidad no encontrada (%s)", locality)
        return _empty_response()
    except (RouteNotFoundError, WeightOutOfRangeError) as e:
        logger.info("Carrier service: %s", e)
        return _empty_response()
    except Exception:
        logger.exception("Carrier service: error inesperado")
        # 404 fuerza "backup rates" del merchant: mejor que cotizar mal.
        return {"statusCode": 404, "body": json.dumps({"error": "internal"})}

    # CLP no tiene subunidades → Shopify exige multiplicar por 100.
    total_minor = int(round(result.total_con_iva_clp)) * 100

    description = (
        f"{result.destination_sucursal} ({result.destination_zona}) — "
        f"{result.kg_cobrable:.0f} kg cobrables"
    )

    rate_obj: dict[str, Any] = {
        "service_name": _service_name(),
        "service_code": _service_code(),
        "total_price": str(total_minor),
        "currency": "CLP",
        "description": description,
    }

    response_body = {"rates": [rate_obj]}
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(response_body),
    }
