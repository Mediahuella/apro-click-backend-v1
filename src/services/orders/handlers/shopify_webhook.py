"""Webhook HTTP API — Shopify orders/* (HMAC + body en bruto)."""
from __future__ import annotations

import base64
import json
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

from services.order_service import upsert_from_shopify_payload  # noqa: E402
from utils.shopify_webhook import (  # noqa: E402
    get_shopify_webhook_secret,
    verify_shopify_webhook,
)

logger = Logger()
tracer = Tracer()


def _raw_body(event: dict) -> bytes:
    body = event.get("body")
    if body is None:
        return b""
    if event.get("isBase64Encoded"):
        if isinstance(body, str):
            return base64.b64decode(body)
        return b""
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(body).encode("utf-8")


def _header(headers: dict | None, name: str) -> str | None:
    if not headers:
        return None
    for k, v in headers.items():
        if k.lower() == name.lower():
            return (v or "").strip() or None
    return None


@tracer.capture_lambda_handler
def lambda_handler(event: dict, context: LambdaContext) -> dict:
    headers = event.get("headers") or {}
    hmac_hdr = _header(headers, "x-shopify-hmac-sha256")
    shop_domain = _header(headers, "x-shopify-shop-domain")
    raw = _raw_body(event)
    topic = _header(headers, "x-shopify-topic") or ""

    if not get_shopify_webhook_secret():
        logger.error("SHOPIFY_CLIENT_SECRET no configurado")
        return {"statusCode": 500, "body": "misconfigured"}

    if not verify_shopify_webhook(raw, hmac_hdr):
        logger.warning("HMAC inválido")
        return {"statusCode": 401, "body": "unauthorized"}

    if topic and not (topic or "").lower().startswith("orders/"):
        return {"statusCode": 200, "body": "ignored"}

    if not shop_domain:
        return {"statusCode": 400, "body": "missing shop domain"}

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError):
        return {"statusCode": 400, "body": "invalid json"}

    if not isinstance(payload, dict) or "id" not in payload:
        return {"statusCode": 200, "body": "ok"}

    try:
        upsert_from_shopify_payload(shop_domain, payload)
    except ValueError as e:
        logger.warning("payload orden: %s", e)
        return {"statusCode": 200, "body": "ok"}
    except Exception:
        logger.exception("Error upsert pedido")
        return {"statusCode": 500, "body": "error"}

    return {"statusCode": 200, "body": "ok"}
