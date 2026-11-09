"""Autenticación del canal público (theme app extension) vía API key + token PDF.

- ``require_quotes_api_key``: el ``POST`` público y los GET storefront exigen el
  header ``X-Api-Key`` si ``QUOTES_PUBLIC_API_KEY`` está configurada.
- ``require_apro_ia_webhook_key``: el webhook de eventos de APRO IA exige el
  header ``x-api-key`` con la clave propia del webhook (distinta de la del
  storefront) si ``APRO_IA_WEBHOOK_API_KEY`` está configurada.
- ``sign_pdf_token`` / ``verify_pdf_token``: HMAC sobre el ``quote_id`` para el
  link de descarga del PDF que va por email (acceso sin login).
- ``sign_pay_token`` / ``verify_pay_token``: HMAC (con prefijo propio) para el
  link «Ir a pagar» de la cuenta de cliente.
"""
from __future__ import annotations

import hashlib
import hmac
import os


def _get_header(headers: dict[str, str] | None, name: str) -> str | None:
    h = headers or {}
    lname = name.lower()
    for k, v in h.items():
        if str(k).lower() == lname:
            return v
    return None


def require_quotes_api_key(headers: dict[str, str] | None) -> None:
    """Si ``QUOTES_PUBLIC_API_KEY`` está vacía en la Lambda, no se exige header."""
    expected = (os.environ.get("QUOTES_PUBLIC_API_KEY") or "").strip()
    if not expected:
        return
    got = _get_header(headers, "x-api-key")
    if not got or got.strip() != expected:
        raise PermissionError("API key inválida o ausente (use header X-Api-Key)")


def require_apro_ia_webhook_key(headers: dict[str, str] | None) -> None:
    """Valida el header ``x-api-key`` del webhook de eventos de APRO IA.

    Si ``APRO_IA_WEBHOOK_API_KEY`` no está configurada, se rechaza siempre —
    a diferencia del canal storefront, este endpoint recibe datos de un
    sistema externo y no debe quedar abierto por omisión.
    """
    expected = (os.environ.get("APRO_IA_WEBHOOK_API_KEY") or "").strip()
    if not expected:
        raise PermissionError("APRO_IA_WEBHOOK_API_KEY no está configurado")
    got = _get_header(headers, "x-api-key")
    if not got or got.strip() != expected:
        raise PermissionError("API key inválida o ausente (use header x-api-key)")


def _link_secret() -> str:
    return (os.environ.get("QUOTES_PDF_LINK_SECRET") or "").strip()


def sign_pdf_token(quote_id: str) -> str:
    """Token HMAC-SHA256 para el link de descarga del PDF (email)."""
    secret = _link_secret()
    if not secret:
        return ""
    return hmac.new(
        secret.encode("utf-8"), str(quote_id).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_pdf_token(quote_id: str, token: str | None) -> bool:
    """Compara en tiempo constante el token contra el HMAC esperado."""
    expected = sign_pdf_token(quote_id)
    if not expected or not token:
        return False
    return hmac.compare_digest(expected, token.strip())


def _pay_payload(quote_id: str) -> str:
    # Prefijo propio: el token del PDF no sirve como token de pago y viceversa.
    return f"pay:{quote_id}"


def sign_pay_token(quote_id: str) -> str:
    """Token HMAC-SHA256 para el link «Ir a pagar» de la cotización."""
    secret = _link_secret()
    if not secret:
        return ""
    return hmac.new(
        secret.encode("utf-8"), _pay_payload(str(quote_id)).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_pay_token(quote_id: str, token: str | None) -> bool:
    """Compara en tiempo constante el token de pago contra el HMAC esperado."""
    expected = sign_pay_token(quote_id)
    if not expected or not token:
        return False
    return hmac.compare_digest(expected, token.strip())
