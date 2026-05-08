"""Cliente Admin GraphQL de Shopify para el servicio prices.

Equivalente al de ``orders/utils/shopify_graphql.py`` (mismo patrón). Lo
duplicamos para no acoplar servicios — cada Lambda empaqueta lo suyo.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def shopify_api_version() -> str:
    return (os.environ.get("SHOPIFY_API_VERSION") or "2026-04").strip()


def graphql_call(
    shop_domain: str,
    access_token: str,
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    timeout: int = 25,
) -> dict[str, Any]:
    """Ejecuta una operation Admin GraphQL y devuelve ``data``.

    Lanza ``ValueError`` con el detalle si la respuesta tiene errores HTTP o
    ``errors`` GraphQL. Los ``userErrors`` por mutación los maneja el caller.
    """
    shop = shop_domain.strip().lower()
    ver = shopify_api_version()
    url = f"https://{shop}/admin/api/{ver}/graphql.json"
    payload = json.dumps({"query": query, "variables": variables or {}}).encode(
        "utf-8"
    )
    req = Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except OSError:
            detail = str(e.code)
        raise ValueError(f"Shopify HTTP {e.code}: {detail[:500]}") from e
    except (OSError, json.JSONDecodeError, UnicodeError) as e:
        raise ValueError(f"Error llamando a Shopify GraphQL: {e}") from e

    if not isinstance(body, dict):
        raise ValueError("Respuesta GraphQL inválida")
    errs = body.get("errors")
    if isinstance(errs, list) and errs:
        parts: list[str] = []
        for e in errs:
            if isinstance(e, dict):
                parts.append(str(e.get("message") or e))
            else:
                parts.append(str(e))
        raise ValueError("Shopify: " + "; ".join(parts)[:2000])
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError("GraphQL sin data")
    return data


def format_user_errors(errs: list[Any]) -> str:
    parts: list[str] = []
    for e in errs:
        if isinstance(e, dict):
            parts.append(str(e.get("message") or e))
        else:
            parts.append(str(e))
    return "; ".join(parts) if parts else "error desconocido"
