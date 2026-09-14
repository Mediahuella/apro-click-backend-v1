"""Cliente del servicio de créditos de APRO: ¿esta empresa tiene crédito?

    GET {APRO_CREDIT_BASE_URL}/credits/client/{rut}
    Authorization: Bearer {APRO_CREDIT_BEARER_TOKEN}

Respuestas del DMS (contrato confirmado por APRO el 14-09-2026):

    200 → {"success": true,  "message": "...", "credit": {"folio": "CR-007700",
                                                          "createdAt": "..."}}
    404 → {"success": false, "message": "No se encontró crédito para el
                                         cliente con RUT: ...", "credit": null}

El ``404`` **no es un error**: es la respuesta legítima para "no tiene
crédito". Lo que sí es indeterminado es un fallo de red, un 5xx o que falte
configuración — ahí devolvemos ``None`` y quien llama debe **no tocar nada**,
porque asumir "sin crédito" le quitaría el medio de pago a un cliente que sí lo
tiene, y asumir "con crédito" se lo daría a quien no.

El endpoint solo informa **existencia** de crédito (folio y fecha), no montos:
no hay cupo autorizado ni disponible en este contrato.

Reusa la configuración de la **API de clientes** de APRO, que es el mismo
servicio (otra ruta del mismo host) y el mismo token:

    APRO_IA_CLIENTS_BASE_URL     — host productivo
    APRO_IA_CLIENTS_BASE_URL_QA  — host de QA (``env="qa"``)
    APRO_IA_BEARER_TOKEN         — token productivo
    APRO_IA_BEARER_TOKEN_QA      — token de QA

Propias de créditos, ambas opcionales:

    APRO_CREDIT_PATH_TEMPLATE  — default ``/credits/client/{rut}``
    APRO_CREDIT_FORCE_RUTS     — RUTs que siempre se consideran con crédito,
                                 separados por coma (empresas de prueba)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from aws_lambda_powertools import Logger

logger = Logger()

_DEFAULT_PATH = "/credits/client/{rut}"


def normalizar_rut(valor: str) -> str:
    """RUT sin puntos, guiones ni espacios, en mayúsculas (``76486379K``).

    El DMS acepta ambas formas, pero normalizamos para comparar contra la
    allowlist y para que el log sea siempre el mismo string.
    """
    return (
        str(valor or "")
        .strip()
        .upper()
        .replace(".", "")
        .replace("-", "")
        .replace(" ", "")
    )


ENV_PROD = "prod"
ENV_QA = "qa"


def _base_url(env: str) -> str:
    clave = (
        "APRO_IA_CLIENTS_BASE_URL_QA" if env == ENV_QA else "APRO_IA_CLIENTS_BASE_URL"
    )
    return (os.environ.get(clave) or "").strip().rstrip("/")


def _token(env: str) -> str:
    clave = "APRO_IA_BEARER_TOKEN_QA" if env == ENV_QA else "APRO_IA_BEARER_TOKEN"
    return (os.environ.get(clave) or "").strip()


def esta_configurado(env: str = ENV_PROD) -> bool:
    return bool(_base_url(env) and _token(env))


def _ruts_forzados() -> set[str]:
    crudo = os.environ.get("APRO_CREDIT_FORCE_RUTS") or ""
    return {normalizar_rut(r) for r in crudo.split(",") if r.strip()}


def _url(rut: str, env: str) -> str:
    base = _base_url(env)
    plantilla = (os.environ.get("APRO_CREDIT_PATH_TEMPLATE") or _DEFAULT_PATH).strip()
    if not plantilla.startswith("/"):
        plantilla = f"/{plantilla}"
    return f"{base}{plantilla.format(rut=urllib.parse.quote(rut, safe=''))}"


def _sin_credito_o_indeterminado(
    error: urllib.error.HTTPError, rut: str
) -> dict[str, Any] | None:
    """Distingue el 404 del DMS ("no tiene crédito") del 404 de ruta inexistente.

    Los dos llegan como HTTP 404 y significan cosas opuestas:

        {"success": false, "message": "No se encontró crédito ...",
         "credit": null}                          → el cliente no tiene crédito
        {"message": "Cannot GET /credits/client/...", "error": "Not Found",
         "statusCode": 404}                       → la ruta no existe en ese host

    Sin esta distinción, apuntar la variable a un host donde el módulo de
    créditos no está desplegado apagaría el crédito de **todas** las empresas
    de una sola pasada. Solo el primer cuerpo cuenta como respuesta del DMS;
    cualquier otro 404 es indeterminado y no cambia nada.
    """
    try:
        cuerpo = json.loads(error.read() or b"{}")
    except Exception:  # noqa: BLE001 — un 404 sin JSON no es respuesta del DMS
        cuerpo = None
    if isinstance(cuerpo, dict) and ("credit" in cuerpo or "success" in cuerpo):
        return {"tiene_credito": False, "folio": None, "creado_en": None}
    logger.warning(
        "404 que no viene del servicio de créditos (¿ruta o host equivocado?): "
        "se trata como indeterminado",
        extra={"rut": rut, "cuerpo": str(cuerpo)[:200]},
    )
    return None


def consultar_credito(
    rut: str, *, env: str = ENV_PROD, timeout: int = 8
) -> dict[str, Any] | None:
    """``{"tiene_credito": bool, "folio": str|None, "creado_en": str|None}``.

    Devuelve ``None`` cuando **no se pudo determinar** (sin configuración, red
    caída, 5xx, respuesta ilegible). Quien llama no debe cambiar nada en ese
    caso. Nunca lanza.
    """
    rut_normalizado = normalizar_rut(rut)
    if not rut_normalizado:
        return None

    if rut_normalizado in _ruts_forzados():
        logger.info(
            "Crédito forzado por APRO_CREDIT_FORCE_RUTS",
            extra={"rut": rut_normalizado},
        )
        return {"tiene_credito": True, "folio": None, "creado_en": None, "forzado": True}

    if not esta_configurado(env):
        return None

    req = urllib.request.Request(
        _url(rut_normalizado, env),
        headers={
            "Authorization": f"Bearer {_token(env)}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return _sin_credito_o_indeterminado(e, rut_normalizado)
        logger.warning(
            "Consulta de crédito a APRO devolvió error HTTP",
            extra={"rut": rut_normalizado, "status": e.code},
        )
        return None
    except Exception:
        logger.warning(
            "No se pudo consultar el crédito en APRO",
            extra={"rut": rut_normalizado},
        )
        return None

    if not isinstance(payload, dict):
        return None
    credito = payload.get("credit")
    if not isinstance(credito, dict):
        # `success: false` sin error HTTP: lo tratamos como "sin crédito" solo
        # si el DMS lo dice explícitamente; cualquier otra forma es indeterminado.
        if payload.get("success") is False:
            return {"tiene_credito": False, "folio": None, "creado_en": None}
        return None
    return {
        "tiene_credito": True,
        "folio": str(credito.get("folio") or "") or None,
        "creado_en": str(credito.get("createdAt") or "") or None,
    }
