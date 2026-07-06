"""Autenticación del canal público (theme app extension) vía API key + token PDF.

- ``require_quotes_api_key``: el ``POST`` público y los GET storefront exigen el
  header ``X-Api-Key`` si ``QUOTES_PUBLIC_API_KEY`` está configurada.
- ``sign_pdf_token`` / ``verify_pdf_token``: HMAC sobre el ``quote_id`` para el
  link de descarga del PDF que va por email (acceso sin login).
"""
from __future__ import annotations

import hashlib
import hmac
import os


def require_quotes_api_key(headers: dict[str, str] | None) -> None:
    """Si ``QUOTES_PUBLIC_API_KEY`` está vacía en la Lambda, no se exige header."""
    expected = (os.environ.get("QUOTES_PUBLIC_API_KEY") or "").strip()
    if not expected:
        return
    h = headers or {}
    got = h.get("x-api-key") or h.get("X-Api-Key") or h.get("x-Api-Key")
    if not got or got.strip() != expected:
        raise PermissionError("API key inválida o ausente (use header X-Api-Key)")


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
