"""Autenticación opcional del canal público (theme extension) vía API key."""
from __future__ import annotations

import os


def require_registration_api_key(headers: dict[str, str] | None) -> None:
    """Si `COMPANY_REGISTRATION_API_KEY` está vacía en la Lambda, no se exige header.

    Cuando tenga valor, el `POST` público requiere `X-Api-Key` coincidente.
    """
    expected = (os.environ.get("COMPANY_REGISTRATION_API_KEY") or "").strip()
    if not expected:
        return
    h = headers or {}
    got = (
        h.get("x-api-key")
        or h.get("X-Api-Key")
        or h.get("x-Api-Key")
    )
    if not got or got.strip() != expected:
        raise PermissionError("API key inválida o ausente (use header X-Api-Key)")
