"""Webhook HTTP API — Shopify ``inventory_levels/update``.

Valida HMAC, garantiza el topic correcto y encola un mensaje en SQS FIFO
para que el worker actualice el metafield ``aproclick.stock`` de la variante
asociada. El handler responde 200 OK lo antes posible (los webhooks de
Shopify se reintegran si tardan más de ~5s).
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

import boto3
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

from utils.shopify_webhook import (  # noqa: E402
    get_shopify_webhook_secret,
    verify_shopify_webhook,
)

logger = Logger()
tracer = Tracer()

_TOPIC = "inventory_levels/update"
_sqs_client = None


def _sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


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
    topic = (_header(headers, "x-shopify-topic") or "").lower()
    if topic and topic != _TOPIC:
        return {"statusCode": 200, "body": "ignored"}

    if not get_shopify_webhook_secret():
        logger.error("SHOPIFY_CLIENT_SECRET no configurado")
        return {"statusCode": 500, "body": "misconfigured"}

    raw = _raw_body(event)
    hmac_hdr = _header(headers, "x-shopify-hmac-sha256")
    if not verify_shopify_webhook(raw, hmac_hdr):
        logger.warning("HMAC inválido en inventory webhook")
        return {"statusCode": 401, "body": "unauthorized"}

    shop_domain = _header(headers, "x-shopify-shop-domain")
    if not shop_domain:
        return {"statusCode": 400, "body": "missing shop domain"}

    queue_url = (os.environ.get("INVENTORY_SYNC_QUEUE_URL") or "").strip()
    if not queue_url:
        logger.error("INVENTORY_SYNC_QUEUE_URL no configurado")
        return {"statusCode": 500, "body": "misconfigured"}

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError):
        return {"statusCode": 400, "body": "invalid json"}

    if not isinstance(payload, dict):
        return {"statusCode": 200, "body": "ok"}

    inventory_item_id = payload.get("inventory_item_id")
    if inventory_item_id is None:
        return {"statusCode": 200, "body": "ok"}

    webhook_id = (
        _header(headers, "x-shopify-webhook-id")
        or _header(headers, "x-shopify-event-id")
        or ""
    )

    message_body = json.dumps(
        {
            "shop_domain": shop_domain,
            "inventory_item_id": inventory_item_id,
            "location_id": payload.get("location_id"),
            "available": payload.get("available"),
            "triggered_at": _header(headers, "x-shopify-triggered-at"),
            "webhook_id": webhook_id or None,
        },
        separators=(",", ":"),
    )

    send_kwargs: dict = {
        "QueueUrl": queue_url,
        "MessageBody": message_body,
        "MessageGroupId": str(inventory_item_id),
    }
    if webhook_id:
        send_kwargs["MessageDeduplicationId"] = webhook_id

    try:
        _sqs().send_message(**send_kwargs)
    except Exception:
        logger.exception("No se pudo encolar inventory_levels/update")
        # 500 para que Shopify reintente y no perdamos el evento.
        return {"statusCode": 500, "body": "enqueue failed"}

    logger.info(
        "inventory webhook — mensaje encolado",
        extra={
            "shop_domain": shop_domain,
            "inventory_item_id": inventory_item_id,
        },
    )
    return {"statusCode": 200, "body": "ok"}
