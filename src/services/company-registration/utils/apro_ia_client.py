"""Cliente HTTP para la API de clientes de APRO IA (DMS de APRO).

Endpoints:
  POST /clients/register     — crear/actualizar un cliente individual
  POST /clients/upsert-excel — carga masiva vía Excel (multipart/form-data)

Reutiliza el mismo patrón que ``services/quotes/services/apro_ia_client.py``
(``urllib.request`` puro, sin dependencias externas, ``Authorization: Bearer``).
Las variables de entorno (``APRO_IA_BASE_URL`` / ``APRO_IA_BEARER_TOKEN``) son las
mismas que usa el servicio ``quotes`` — es el mismo proveedor (DMS de APRO).
"""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from typing import Any

logger = None
try:
    from aws_lambda_powertools import Logger

    logger = Logger()
except ImportError:  # el script de backfill corre fuera de Lambda
    import logging

    logger = logging.getLogger(__name__)


def _base_url() -> str:
    # Host distinto al de la API de cotizaciones (dms.apro.cl): la API de
    # clientes vive en clientes.apro.cl / cliente-qa.apro.cl. El token Bearer
    # sí es compartido con el servicio `quotes` (APRO_IA_BEARER_TOKEN).
    url = (os.environ.get("APRO_IA_CLIENTS_BASE_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("APRO_IA_CLIENTS_BASE_URL no está configurado en el entorno")
    return url


def _token() -> str:
    tok = (os.environ.get("APRO_IA_BEARER_TOKEN") or "").strip()
    if not tok:
        raise RuntimeError("APRO_IA_BEARER_TOKEN no está configurado en el entorno")
    return tok


def registrar_cliente(
    *,
    rut: str,
    name: str,
    phone: str,
    email: str,
    source: str = "AproClick",
) -> dict[str, Any]:
    """Crea/actualiza un cliente en APRO IA. Devuelve el body de la respuesta.

    ``rut`` debe venir sin puntos ni guion, con dígito verificador
    (usar ``utils.rut.normalize_rut_digits``).
    """
    body = {
        "rut": rut,
        "name": name,
        "phone": phone,
        "email": email,
        "source": source,
    }
    url = f"{_base_url()}/clients/register"
    data = json.dumps(body).encode()
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            msg = json.loads(raw).get("message") or raw.decode(errors="replace")
        except Exception:
            msg = raw.decode(errors="replace")
        raise RuntimeError(f"APRO IA registrar_cliente HTTP {exc.code}: {msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"APRO IA registrar_cliente error de red: {exc}") from exc


def upsert_excel(file_bytes: bytes, filename: str = "clientes.xlsx") -> dict[str, Any]:
    """Sube un Excel para carga masiva de clientes. Devuelve {created, updated, errors}."""
    boundary = uuid.uuid4().hex
    content_type = mimetypes.guess_type(filename)[0] or (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()

    url = f"{_base_url()}/clients/upsert-excel"
    headers = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            msg = json.loads(raw).get("message") or raw.decode(errors="replace")
        except Exception:
            msg = raw.decode(errors="replace")
        raise RuntimeError(f"APRO IA upsert_excel HTTP {exc.code}: {msg}") from exc
    except Exception as exc:
        raise RuntimeError(f"APRO IA upsert_excel error de red: {exc}") from exc
