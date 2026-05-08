"""Admin GraphQL — CRUD de Shopify CarrierService.

Sólo se invoca desde el handler administrativo (``handlers/shopify_register``).
El callback de checkout (``handlers/shopify_carrier_service``) NO importa este
módulo, así no carga SQLAlchemy en su cold start.

Scopes Shopify requeridos en ``shopify.app.toml``:

- ``write_shipping`` — crear, actualizar y borrar.
- ``read_shipping`` — listar.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sqlalchemy import select

service_root = Path(__file__).resolve().parent.parent
if str(service_root) not in sys.path:
    sys.path.insert(0, str(service_root))

lambda_root = "/var/task"
if lambda_root not in sys.path:
    sys.path.insert(0, lambda_root)

possible_shared_paths = [
    Path("/var/task/shared"),
    service_root / "shared",
]
for path in possible_shared_paths:
    if path.exists() and (path / "database").exists():
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        break

from database.engine import get_session  # noqa: E402
from database.models.shopify import ShopifyAppInstallation  # noqa: E402

_CARRIER_SERVICE_CREATE = """
mutation CarrierServiceCreate($input: DeliveryCarrierServiceCreateInput!) {
  carrierServiceCreate(input: $input) {
    carrierService {
      id
      name
      callbackUrl
      active
      supportsServiceDiscovery
    }
    userErrors { field message }
  }
}
"""

_CARRIER_SERVICE_UPDATE = """
mutation CarrierServiceUpdate($input: DeliveryCarrierServiceUpdateInput!) {
  carrierServiceUpdate(input: $input) {
    carrierService {
      id
      name
      callbackUrl
      active
      supportsServiceDiscovery
    }
    userErrors { field message }
  }
}
"""

_CARRIER_SERVICE_DELETE = """
mutation CarrierServiceDelete($id: ID!) {
  carrierServiceDelete(id: $id) {
    deletedId
    userErrors { field message }
  }
}
"""

_CARRIER_SERVICES_LIST = """
query CarrierServices($first: Int!, $query: String) {
  carrierServices(first: $first, query: $query) {
    edges {
      node {
        id
        name
        callbackUrl
        active
        supportsServiceDiscovery
      }
    }
  }
}
"""


def _api_version() -> str:
    return (os.environ.get("SHIPPING_SHOPIFY_API_VERSION") or "2026-04").strip()


def _normalize_shop_domain(domain: str) -> str:
    d = (domain or "").strip().lower()
    if not d:
        raise ValueError("shop_domain vacío")
    if not d.endswith(".myshopify.com") and "." in d:
        raise ValueError(
            "Dominio inválido: usar el formato tienda.myshopify.com"
        )
    if not d.endswith(".myshopify.com"):
        d = f"{d}.myshopify.com"
    return d


def resolve_shop_and_token(shop_domain: str | None) -> tuple[str, str]:
    """Devuelve ``(shop, access_token)`` desde ``shopify_app_installations``.

    Si ``shop_domain`` viene vacío, toma la última instalación activa con
    token. Lanza ``LookupError`` si no hay ninguna.
    """
    with get_session() as session:
        if shop_domain:
            dom = _normalize_shop_domain(shop_domain)
            row = session.scalar(
                select(ShopifyAppInstallation).where(
                    ShopifyAppInstallation.shop_domain == dom,
                    ShopifyAppInstallation.uninstalled_at.is_(None),
                )
            )
            if not row or not row.shopify_access_token:
                raise LookupError(
                    f"No hay instalación activa con token para '{dom}'"
                )
            return row.shop_domain, row.shopify_access_token.strip()

        row = session.scalar(
            select(ShopifyAppInstallation)
            .where(
                ShopifyAppInstallation.uninstalled_at.is_(None),
                ShopifyAppInstallation.shopify_access_token.is_not(None),
            )
            .order_by(ShopifyAppInstallation.installed_at.desc())
        )
        if not row or not row.shopify_access_token:
            raise LookupError(
                "No hay ninguna instalación de Shopify con token; "
                "complete OAuth o indique shop_domain."
            )
        return row.shop_domain, row.shopify_access_token.strip()


def _graphql(
    shop: str, token: str, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    url = f"https://{shop}/admin/api/{_api_version()}/graphql.json"
    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token,
        },
    )
    try:
        with urlopen(req, timeout=20) as resp:
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
        msgs = [str(e.get("message") or e) if isinstance(e, dict) else str(e) for e in errs]
        raise ValueError("Shopify: " + "; ".join(msgs)[:2000])
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError("GraphQL sin data")
    return data


def _format_user_errors(errs: list[Any]) -> str:
    parts = []
    for e in errs:
        if isinstance(e, dict):
            parts.append(str(e.get("message") or e))
        else:
            parts.append(str(e))
    return "; ".join(parts) if parts else "error desconocido"


def list_carrier_services(
    shop_domain: str | None = None, *, query: str | None = None, first: int = 25
) -> list[dict[str, Any]]:
    """Lista carrier services de la tienda. Requiere ``read_shipping``."""
    shop, token = resolve_shop_and_token(shop_domain)
    data = _graphql(
        shop,
        token,
        _CARRIER_SERVICES_LIST,
        {"first": int(first), "query": query},
    )
    edges = (data.get("carrierServices") or {}).get("edges") or []
    return [edge["node"] for edge in edges if isinstance(edge, dict) and edge.get("node")]


def create_carrier_service(
    *,
    name: str,
    callback_url: str,
    supports_service_discovery: bool = True,
    active: bool = True,
    shop_domain: str | None = None,
) -> dict[str, Any]:
    """Crea un CarrierService. Requiere ``write_shipping``.

    Sólo la app que crea el servicio puede actualizarlo más adelante.
    """
    if not (name or "").strip():
        raise ValueError("name es requerido")
    if not (callback_url or "").strip():
        raise ValueError("callback_url es requerido")
    shop, token = resolve_shop_and_token(shop_domain)
    data = _graphql(
        shop,
        token,
        _CARRIER_SERVICE_CREATE,
        {
            "input": {
                "name": name.strip(),
                "callbackUrl": callback_url.strip(),
                "supportsServiceDiscovery": bool(supports_service_discovery),
                "active": bool(active),
            }
        },
    )
    payload = data.get("carrierServiceCreate") or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        raise ValueError(
            "carrierServiceCreate falló: " + _format_user_errors(user_errors)
        )
    cs = payload.get("carrierService")
    if not isinstance(cs, dict):
        raise ValueError("carrierServiceCreate sin carrierService en la respuesta")
    return cs


def update_carrier_service(
    *,
    carrier_service_id: str,
    name: str | None = None,
    callback_url: str | None = None,
    active: bool | None = None,
    shop_domain: str | None = None,
) -> dict[str, Any]:
    """Modifica un CarrierService existente. Requiere ``write_shipping``."""
    cid = (carrier_service_id or "").strip()
    if not cid:
        raise ValueError("carrier_service_id es requerido")
    if not cid.startswith("gid://shopify/DeliveryCarrierService/"):
        cid = f"gid://shopify/DeliveryCarrierService/{cid}"

    inp: dict[str, Any] = {"id": cid}
    if name is not None:
        if not str(name).strip():
            raise ValueError("name no puede estar vacío")
        inp["name"] = str(name).strip()
    if callback_url is not None:
        if not str(callback_url).strip():
            raise ValueError("callback_url no puede estar vacío")
        inp["callbackUrl"] = str(callback_url).strip()
    if active is not None:
        if not isinstance(active, bool):
            raise ValueError("active debe ser booleano")
        inp["active"] = active
    if len(inp) <= 1:
        raise ValueError("nada para actualizar")

    shop, token = resolve_shop_and_token(shop_domain)
    data = _graphql(shop, token, _CARRIER_SERVICE_UPDATE, {"input": inp})
    payload = data.get("carrierServiceUpdate") or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        raise ValueError(
            "carrierServiceUpdate falló: " + _format_user_errors(user_errors)
        )
    cs = payload.get("carrierService")
    if not isinstance(cs, dict):
        raise ValueError("carrierServiceUpdate sin carrierService en la respuesta")
    return cs


def delete_carrier_service(
    *,
    carrier_service_id: str,
    shop_domain: str | None = None,
) -> str:
    """Elimina un CarrierService. Requiere ``write_shipping``."""
    cid = (carrier_service_id or "").strip()
    if not cid:
        raise ValueError("carrier_service_id es requerido")
    if not cid.startswith("gid://shopify/DeliveryCarrierService/"):
        cid = f"gid://shopify/DeliveryCarrierService/{cid}"

    shop, token = resolve_shop_and_token(shop_domain)
    data = _graphql(shop, token, _CARRIER_SERVICE_DELETE, {"id": cid})
    payload = data.get("carrierServiceDelete") or {}
    user_errors = payload.get("userErrors") or []
    if user_errors:
        raise ValueError(
            "carrierServiceDelete falló: " + _format_user_errors(user_errors)
        )
    deleted_id = payload.get("deletedId") or ""
    return str(deleted_id)
