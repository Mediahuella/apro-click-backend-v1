"""Cliente HTTP para la API APRO IA (DMS de APRO).

Endpoints:
  POST   /quotation/aproclick              — crear cotización
  GET    /quotation/aproclick/{folio}      — obtener cotización
  PATCH  /quotation/aproclick/{folio}      — editar productos / comentario
  GET    /quotation/aproclick/download/{folio} — descargar PDF (bytes)

Observaciones del ambiente QA (verificadas con pruebas reales):
  - La creación responde HTTP 201, no 200.
  - El GET usa nombres de campo distintos al POST/PATCH:
      net_amount   → sale_amount
      total_amount → total_with_iva
  - seller_email debe ser un usuario registrado en el DMS de APRO.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from aws_lambda_powertools import Logger

logger = Logger()


#: Ambiente productivo del DMS. Es el default de todas las funciones: ningún
#: llamador existente cambia de comportamiento al agregarse el soporte de QA.
ENV_PROD = "prod"
#: Ambiente de pruebas del DMS (``dms-qa.apro.cl``). Solo se usa para las
#: empresas de la allowlist ``APRO_IA_QA_ALLOWED_RUTS`` — ver
#: ``quote_service._espejar_en_qa``.
ENV_QA = "qa"

#: Sufijo de las variables de entorno por ambiente. QA usa credenciales
#: propias: el bearer token de producción devuelve ``403 Token inválido``
#: contra ``dms-qa.apro.cl`` (verificado), no son intercambiables.
_ENV_SUFFIX = {ENV_PROD: "", ENV_QA: "_QA"}


def _env_var(nombre: str, env: str) -> str:
    try:
        sufijo = _ENV_SUFFIX[env]
    except KeyError:
        raise ValueError(f"Ambiente APRO IA desconocido: {env}") from None
    return f"{nombre}{sufijo}"


def _base_url(env: str = ENV_PROD) -> str:
    var = _env_var("APRO_IA_BASE_URL", env)
    url = (os.environ.get(var) or "").strip().rstrip("/")
    if not url:
        raise RuntimeError(f"{var} no está configurado en el entorno")
    return url


def _token(env: str = ENV_PROD) -> str:
    var = _env_var("APRO_IA_BEARER_TOKEN", env)
    tok = (os.environ.get(var) or "").strip()
    if not tok:
        raise RuntimeError(f"{var} no está configurado en el entorno")
    return tok


def env_configurado(env: str) -> bool:
    """``True`` si ese ambiente tiene base URL y token cargados.

    Permite deployar con QA sin credenciales todavía: el espejo se saltea en
    silencio en vez de fallar en cada cotización.
    """
    try:
        return bool(_base_url(env) and _token(env))
    except (RuntimeError, ValueError):
        return False


def _request(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    timeout: int = 15,
    env: str = ENV_PROD,
) -> tuple[int, Any]:
    """Realiza la petición y devuelve (status_code, parsed_body).

    Para respuestas application/pdf devuelve (status, bytes).
    Lanza RuntimeError en errores HTTP o de red.
    """
    url = f"{_base_url(env)}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {_token(env)}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "")
            if "application/pdf" in ct:
                return resp.status, raw
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
            msg = payload.get("message") or str(payload)
        except Exception:
            msg = raw.decode(errors="replace")
        raise RuntimeError(f"APRO IA[{env}] HTTP {exc.code}: {msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"APRO IA[{env}] error de red: {exc}") from exc


def _normalize_response(data: dict) -> dict:
    """Normaliza los campos que el GET responde con nombres distintos al POST/PATCH."""
    if "sale_amount" in data and "net_amount" not in data:
        data["net_amount"] = data.pop("sale_amount")
    if "total_with_iva" in data and "total_amount" not in data:
        data["total_amount"] = data.pop("total_with_iva")
    return data


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------

def crear_cotizacion(
    *,
    client_rut: str,
    client_nombre: str,
    seller_email: str,
    seller_sap_code: str,
    productos: list[dict[str, Any]],
    comment: str | None = None,
    env: str = ENV_PROD,
) -> dict[str, Any]:
    """Crea una cotización en APRO IA y devuelve el objeto ``data``.

    APRO IA identifica al vendedor por ``seller_sap_code``, no por
    ``seller_email`` (sus emails de vendedor no siempre coinciden con los
    nuestros) — se envían ambos, pero el código SAP es la clave real.
    """
    body: dict[str, Any] = {
        # El PDF de APRO IA documenta "nombre", pero su API real (y el GET/webhook) usan "name".
        "client": {"rut": client_rut, "name": client_nombre},
        "seller_email": seller_email,
        "seller_sap_code": seller_sap_code,
        "products": productos,
    }
    if comment:
        body["comment"] = comment

    status, resp = _request("POST", "/quotation/aproclick", body, env=env)
    if status not in (200, 201) or not resp.get("success"):
        raise RuntimeError(f"APRO IA crear falló (status {status}): {resp.get('message')}")
    return _normalize_response(resp["data"])


def obtener_cotizacion(folio: str, *, env: str = ENV_PROD) -> dict[str, Any]:
    """Devuelve el objeto ``data`` de la cotización con ese folio."""
    _, resp = _request("GET", f"/quotation/aproclick/{folio}", env=env)
    if not resp.get("success"):
        raise RuntimeError(f"APRO IA obtener falló: {resp.get('message')}")
    return _normalize_response(resp["data"])


def editar_cotizacion(
    folio: str,
    *,
    productos: list[dict[str, Any]],
    comment: str | None = None,
    env: str = ENV_PROD,
) -> dict[str, Any]:
    """Actualiza productos y comentario de una cotización existente. Devuelve ``data``."""
    body: dict[str, Any] = {"products": productos}
    if comment is not None:
        body["comment"] = comment

    status, resp = _request("PATCH", f"/quotation/aproclick/{folio}", body, env=env)
    if status not in (200, 201) or not resp.get("success"):
        raise RuntimeError(f"APRO IA editar falló (status {status}): {resp.get('message')}")
    return _normalize_response(resp["data"])


def descargar_pdf(folio: str, *, env: str = ENV_PROD) -> bytes:
    """Descarga el PDF de la cotización y devuelve los bytes raw."""
    status, raw = _request(
        "GET", f"/quotation/aproclick/download/{folio}", timeout=20, env=env
    )
    if not isinstance(raw, bytes):
        raise RuntimeError(f"APRO IA download: respuesta inesperada (status {status})")
    if not raw.startswith(b"%PDF"):
        raise RuntimeError("APRO IA download: el contenido no es un PDF válido")
    return raw


#: SKU con que APRO cobra el despacho en sus cotizaciones («COSTO DE ENVIO»,
#: visto en el DMS). El envío de la cotización viaja como un producto más con
#: este SKU; al volver por el webhook se reconoce por el mismo SKU y no se
#: trata como una línea de catálogo.
DEFAULT_SHIPPING_SKU = "00000300000013"
SHIPPING_NAME = "COSTO DE ENVIO"


def shipping_sku() -> str:
    return (os.environ.get("APRO_IA_SHIPPING_SKU") or "").strip() or DEFAULT_SHIPPING_SKU


def is_shipping_sku(sku: Any) -> bool:
    return str(sku or "").strip().lower() == shipping_sku().lower()


# Nombre del campo de comentario por producto en el contrato de APRO IA.
LINE_COMMENT_FIELD = "comment_product"


def line_comments_enabled() -> bool:
    """``True`` si el comentario de cada línea se manda a APRO IA.

    APRO IA recibe el comentario de cada producto en
    ``products[].comment_product`` (``LINE_COMMENT_FIELD``); se manda solo con
    ``APRO_IA_LINE_COMMENTS=true``,
    para no arriesgar que un DMS que no lo conozca rechace la cotización
    completa. La lectura desde el webhook está siempre activa: si APRO IA no
    lo manda, no cambia nada.
    """
    return (os.environ.get("APRO_IA_LINE_COMMENTS") or "").strip().lower() in (
        "1", "true", "yes", "si", "sí",
    )


def _producto(sku: str, name: str, unit: Any, qty: int) -> dict[str, Any]:
    from decimal import Decimal

    unit = Decimal(unit)
    return {
        "sku": sku,
        "name": name,
        "price": int(unit.quantize(Decimal("1"))),
        "amount": qty,
        "total": int((unit * qty).quantize(Decimal("1"))),
    }


def build_productos(
    lines: list[Any],
    shipping_line: dict | None = None,
    *,
    include_comments: bool | None = None,
) -> list[dict[str, Any]]:
    """Convierte líneas de QuoteRequest al formato de productos de APRO IA.

    Si la cotización tiene envío, se agrega al final como el producto
    ``COSTO DE ENVIO`` (``shipping_sku()``), con el precio neto del envío.
    """
    from decimal import Decimal, InvalidOperation

    if include_comments is None:
        include_comments = line_comments_enabled()

    result = []
    for ln in sorted(lines, key=lambda x: x.line_number or 0):
        unit = ln.negotiated_unit_price
        if unit is None:
            unit = ln.requested_unit_price
        if unit is None:
            unit = Decimal("0")
        producto = _producto(
            ln.sku or "", ln.title or ln.sku or "", unit, int(ln.quantity or 1)
        )
        comment = (getattr(ln, "comment", None) or "").strip()
        if include_comments and comment:
            producto[LINE_COMMENT_FIELD] = comment
        result.append(producto)

    if isinstance(shipping_line, dict) and shipping_line.get("title"):
        try:
            price = Decimal(str(shipping_line.get("price") or "0"))
        except (InvalidOperation, ValueError):
            price = None
        if price is not None:
            title = str(shipping_line["title"]).strip()
            name = f"{SHIPPING_NAME} - {title}" if title else SHIPPING_NAME
            result.append(_producto(shipping_sku(), name, price, 1))
    return result
