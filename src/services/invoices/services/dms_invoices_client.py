"""Cliente HTTP para el DMS de APRO — consulta de facturas por Orden de Compra.

Endpoint: GET {base}/search-document/by-oc?oc={folio}&rut={rut}
Autenticación: Bearer token fijo (ver docs/Consulta de Facturas por OC.pdf).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from aws_lambda_powertools import Logger

logger = Logger()


def _base_url() -> str:
    # Mismo host que usa `quotes` para las cotizaciones: es el único DMS de
    # APRO. Antes esto tenía su propia variable (`DMS_INVOICES_BASE_URL`) con
    # el mismo valor, y dos variables para un solo host solo dan lugar a que se
    # desincronicen.
    url = (os.environ.get("APRO_IA_BASE_URL") or "https://dms.apro.cl").strip().rstrip("/")
    if not url:
        raise RuntimeError("APRO_IA_BASE_URL no está configurado en el entorno")
    return url


def _token() -> str:
    # Mismo token Bearer compartido con los servicios quotes/company-registration
    # (distinto host, mismo DMS de APRO). Ver docs/Consulta de Facturas por OC.pdf.
    tok = (os.environ.get("APRO_IA_BEARER_TOKEN") or "").strip()
    if not tok:
        raise RuntimeError("APRO_IA_BEARER_TOKEN no está configurado en el entorno")
    return tok


def consultar_factura_por_oc(folio: str, rut: str, *, timeout: int = 15) -> dict[str, Any]:
    """Consulta la factura y el comprobante de recepción asociados a una OC.

    Lanza ``LookupError`` si el DMS responde 404 (OC sin documentos).
    Lanza ``RuntimeError`` para otros errores HTTP o de red.
    """
    query = urllib.parse.urlencode({"oc": folio, "rut": rut})
    url = f"{_base_url()}/search-document/by-oc?{query}"
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Accept": "application/json",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
            msg = payload.get("message") or str(payload)
        except Exception:
            msg = raw.decode(errors="replace")
        if exc.code == 404:
            raise LookupError(msg) from exc
        raise RuntimeError(f"DMS facturas HTTP {exc.code}: {msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"DMS facturas error de red: {exc}") from exc


def consultar_facturas_por_ocs(
    ocs: list[dict[str, str]], rut: str, *, timeout: int = 10, max_workers: int = 8
) -> list[dict[str, Any]]:
    """Consulta el DMS en paralelo para una lista de OC (``{quote_folio, oc_folio}``).

    Devuelve un item por OC con ``status``: ``"found"`` (con ``invoice``),
    ``"not_found"`` (el DMS no tiene documentos todavía) o ``"error"``.
    """

    def _one(oc: dict[str, str]) -> dict[str, Any]:
        base = {
            "quote_folio": oc.get("quote_folio"),
            "oc_folio": oc.get("oc_folio"),
            "order_name": oc.get("order_name"),
        }
        try:
            data = consultar_factura_por_oc(oc["oc_folio"], rut, timeout=timeout)
            return {**base, "status": "found", "invoice": data}
        except LookupError:
            return {**base, "status": "not_found", "invoice": None}
        except Exception:
            logger.exception(f"Error consultando OC {oc.get('oc_folio')} en DMS")
            return {**base, "status": "error", "invoice": None}

    if not ocs:
        return []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(_one, ocs))
