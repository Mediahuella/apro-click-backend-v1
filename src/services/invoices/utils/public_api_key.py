"""Autenticación del canal público storefront (theme app extension).

``require_invoices_public_api_key``: el endpoint ``/api/v1/storefront/invoices/*``
exige el header ``X-Api-Key`` si ``INVOICES_PUBLIC_API_KEY`` está configurada.
Mismo patrón que ``companies/utils/public_api_key.py`` y ``quotes/utils/quotes_api_key.py``.
"""
from __future__ import annotations

import os


def _get_header(headers: dict[str, str] | None, name: str) -> str | None:
    h = headers or {}
    lname = name.lower()
    for k, v in h.items():
        if str(k).lower() == lname:
            return v
    return None


def require_invoices_public_api_key(headers: dict[str, str] | None) -> None:
    """Si ``INVOICES_PUBLIC_API_KEY`` está vacía en la Lambda, no se exige header."""
    expected = (os.environ.get("INVOICES_PUBLIC_API_KEY") or "").strip()
    if not expected:
        return
    got = _get_header(headers, "x-api-key")
    if not got or got.strip() != expected:
        raise PermissionError("API key inválida o ausente (use header X-Api-Key)")
