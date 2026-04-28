"""Canal theme / storefront: header X-Api-Key opcional (igual patrón que company-registration)."""
from __future__ import annotations

import os


def require_chat_storefront_key(headers: dict[str, str] | None) -> None:
    expected = (os.environ.get("CHAT_STOREFRONT_API_KEY") or "").strip()
    if not expected:
        return
    h = headers or {}
    got = h.get("x-api-key") or h.get("X-Api-Key") or h.get("x-Api-Key")
    if not got or got.strip() != expected:
        raise PermissionError("API key inválida o ausente (use header X-Api-Key)")
