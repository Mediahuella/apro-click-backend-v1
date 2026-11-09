"""Link estable para solicitar crédito a APRO desde un correo.

APRO genera un identificador nuevo en cada ``POST /credits/url`` e invalida el
anterior, así que un link de APRO guardado en un correo deja de servir apenas
se pide otro (por ejemplo, cuando el cliente entra al checkout). Por eso el
correo lleva un link propio, que no cambia:

    {CREDIT_LINK_BASE_URL}/api/v1/storefront/company/credit/go?t=<token>

y al hacer clic el servicio ``companies`` pide a APRO un link recién generado y
redirige ahí.

El token es ``<payload>.<firma>`` en base64url: el payload es
``<company_id>.<vencimiento unix>`` y la firma un HMAC-SHA256 con
``CREDIT_LINK_SECRET``. Solo identifica a la empresa por su UUID interno: no
lleva el RUT ni permite pedir el link de otra empresa.

Variables de entorno:
  CREDIT_LINK_SECRET    — secreto del HMAC; el mismo en ``companies`` (verifica)
                          y en ``company-registration`` (firma)
  CREDIT_LINK_BASE_URL  — URL de la API de ``companies``, sin slash final
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid

#: Un correo de bienvenida se puede abrir meses después.
VIGENCIA_SEGUNDOS = 365 * 24 * 3600

_RUTA = "/api/v1/storefront/company/credit/go"


def _secreto() -> bytes:
    return (os.environ.get("CREDIT_LINK_SECRET") or "").strip().encode("utf-8")


def _b64(datos: bytes) -> str:
    return base64.urlsafe_b64encode(datos).decode("ascii").rstrip("=")


def _desde_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def _firma(payload: str, secreto: bytes) -> str:
    return _b64(hmac.new(secreto, payload.encode("utf-8"), hashlib.sha256).digest())


def firmar_token(company_id: str, *, ahora: float | None = None) -> str | None:
    """Token firmado para la empresa. ``None`` si falta ``CREDIT_LINK_SECRET``."""
    secreto = _secreto()
    if not secreto:
        return None
    vence = int((ahora if ahora is not None else time.time()) + VIGENCIA_SEGUNDOS)
    payload = f"{uuid.UUID(str(company_id))}.{vence}"
    return f"{_b64(payload.encode('utf-8'))}.{_firma(payload, secreto)}"


def verificar_token(token: str, *, ahora: float | None = None) -> str | None:
    """UUID de la empresa si el token es auténtico y vigente; si no, ``None``."""
    secreto = _secreto()
    if not secreto or not isinstance(token, str) or token.count(".") != 1:
        return None
    payload_b64, firma = token.split(".")
    try:
        payload = _desde_b64(payload_b64).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not hmac.compare_digest(firma, _firma(payload, secreto)):
        return None
    company_id, _, vence = payload.partition(".")
    try:
        if int(vence) < (ahora if ahora is not None else time.time()):
            return None
        return str(uuid.UUID(company_id))
    except ValueError:
        return None


def url_solicitud_credito(company_id: str) -> str | None:
    """Link estable del correo. ``None`` si falta el secreto o la URL base."""
    base = (os.environ.get("CREDIT_LINK_BASE_URL") or "").strip().rstrip("/")
    token = firmar_token(company_id)
    if not base or not token:
        return None
    return f"{base}{_RUTA}?t={token}"
