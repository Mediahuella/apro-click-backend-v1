"""Validación HMAC de webhooks Shopify (X-Shopify-Hmac-Sha256)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os


def get_shopify_webhook_secret() -> str:
    return (os.environ.get("SHOPIFY_CLIENT_SECRET") or "").strip()


def verify_shopify_webhook(body: bytes, hmac_header: str | None) -> bool:
    if not hmac_header:
        return False
    secret = get_shopify_webhook_secret()
    if not secret:
        return False
    digest = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, hmac_header.strip())
