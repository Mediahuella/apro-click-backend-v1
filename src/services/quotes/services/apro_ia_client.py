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


def _base_url() -> str:
    url = (os.environ.get("APRO_IA_BASE_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("APRO_IA_BASE_URL no está configurado en el entorno")
    return url


def _token() -> str:
    tok = (os.environ.get("APRO_IA_BEARER_TOKEN") or "").strip()
    if not tok:
        raise RuntimeError("APRO_IA_BEARER_TOKEN no está configurado en el entorno")
    return tok


def _request(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    timeout: int = 15,
) -> tuple[int, Any]:
    """Realiza la petición y devuelve (status_code, parsed_body).

    Para respuestas application/pdf devuelve (status, bytes).
    Lanza RuntimeError en errores HTTP o de red.
    """
    url = f"{_base_url()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {_token()}",
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
        raise RuntimeError(f"APRO IA HTTP {exc.code}: {msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"APRO IA error de red: {exc}") from exc


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
    productos: list[dict[str, Any]],
    comment: str | None = None,
) -> dict[str, Any]:
    """Crea una cotización en APRO IA y devuelve el objeto ``data``."""
    body: dict[str, Any] = {
        "client": {"rut": client_rut, "nombre": client_nombre},
        "seller_email": seller_email,
        "products": productos,
    }
    if comment:
        body["comment"] = comment

    status, resp = _request("POST", "/quotation/aproclick", body)
    if status not in (200, 201) or not resp.get("success"):
        raise RuntimeError(f"APRO IA crear falló (status {status}): {resp.get('message')}")
    return _normalize_response(resp["data"])


def obtener_cotizacion(folio: str) -> dict[str, Any]:
    """Devuelve el objeto ``data`` de la cotización con ese folio."""
    _, resp = _request("GET", f"/quotation/aproclick/{folio}")
    if not resp.get("success"):
        raise RuntimeError(f"APRO IA obtener falló: {resp.get('message')}")
    return _normalize_response(resp["data"])


def editar_cotizacion(
    folio: str,
    *,
    productos: list[dict[str, Any]],
    comment: str | None = None,
) -> dict[str, Any]:
    """Actualiza productos y comentario de una cotización existente. Devuelve ``data``."""
    body: dict[str, Any] = {"products": productos}
    if comment is not None:
        body["comment"] = comment

    status, resp = _request("PATCH", f"/quotation/aproclick/{folio}", body)
    if status not in (200, 201) or not resp.get("success"):
        raise RuntimeError(f"APRO IA editar falló (status {status}): {resp.get('message')}")
    return _normalize_response(resp["data"])


def descargar_pdf(folio: str) -> bytes:
    """Descarga el PDF de la cotización y devuelve los bytes raw."""
    status, raw = _request("GET", f"/quotation/aproclick/download/{folio}", timeout=20)
    if not isinstance(raw, bytes):
        raise RuntimeError(f"APRO IA download: respuesta inesperada (status {status})")
    if not raw.startswith(b"%PDF"):
        raise RuntimeError("APRO IA download: el contenido no es un PDF válido")
    return raw


def build_productos(lines: list[Any]) -> list[dict[str, Any]]:
    """Convierte líneas de QuoteRequest al formato de productos de APRO IA."""
    from decimal import Decimal

    result = []
    for ln in lines:
        unit = ln.negotiated_unit_price
        if unit is None:
            unit = ln.requested_unit_price
        if unit is None:
            unit = Decimal("0")
        unit = Decimal(unit)
        qty = int(ln.quantity or 1)
        total = int((unit * qty).quantize(Decimal("1")))
        result.append({
            "sku": ln.sku or "",
            "name": ln.title or ln.sku or "",
            "price": int(unit.quantize(Decimal("1"))),
            "amount": qty,
            "total": total,
        })
    return result
