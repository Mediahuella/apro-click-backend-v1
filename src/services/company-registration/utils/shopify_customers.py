"""Admin API GraphQL de Shopify: clientes y empresas B2B (sin REST).

El **access token** de Admin API se obtiene únicamente de PostgreSQL, tabla
`shopify_app_installations`, campo `shopify_access_token` (modelo `ShopifyAppInstallation`).
No se usa token desde variables de entorno ni desde fuera de esa fila de instalación.

Empresas B2B (`companyCreate`): requiere tienda **Shopify Plus** y scopes
`write_companies` y/o `write_customers` (ver docs actuales de Shopify).
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

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

# Validado con MCP validate_graphql_codeblocks (Admin API). Scopes: write_customers, read_customers
MUTATION_CUSTOMER_CREATE = """
mutation customerCreate($input: CustomerInput!) {
  customerCreate(input: $input) {
    userErrors {
      field
      message
    }
    customer {
      id
    }
  }
}
"""

QUERY_CUSTOMERS = """
query findCustomers($first: Int!, $query: String!) {
  customers(first: $first, query: $query) {
    edges {
      node {
        id
      }
    }
  }
}
"""

MUTATION_COMPANY_CREATE = """
mutation CompanyCreate($input: CompanyCreateInput!) {
  companyCreate(input: $input) {
    company {
      id
      name
      mainContact {
        id
        customer {
          id
        }
      }
      locations(first: 5) {
        edges {
          node {
            id
            name
            shippingAddress {
              firstName
              lastName
              address1
              city
              province
              zip
              country
            }
          }
        }
      }
    }
    userErrors {
      field
      message
      code
    }
  }
}
"""

MUTATION_COMPANY_ASSIGN_CUSTOMER = """
mutation CompanyAssignCustomerAsContact($companyId: ID!, $customerId: ID!) {
  companyAssignCustomerAsContact(companyId: $companyId, customerId: $customerId) {
    companyContact {
      id
      customer {
        id
      }
    }
    userErrors {
      field
      message
      code
    }
  }
}
"""


def _normalize_shop_domain(domain: str) -> str:
    d = domain.strip().lower()
    if not d.endswith(".myshopify.com"):
        if "." in d:
            raise ValueError(
                "Dominio de tienda inválido: use el formato tienda.myshopify.com"
            )
        d = f"{d}.myshopify.com"
    return d


def resolve_shop_and_token(shop_domain: str | None) -> tuple[str, str]:
    """Lee la instalación en BD y devuelve (shop_domain, shopify_access_token).

    Origen obligatorio: fila en `shopify_app_installations` con token persistido
    (OAuth del servicio shopify escribe en `shopify_access_token`).
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
                    f"No hay instalación activa con token para la tienda '{dom}'"
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
                "complete OAuth o indique shop_domain en la solicitud."
            )
        return row.shop_domain, row.shopify_access_token.strip()


def _api_version() -> str:
    return (os.environ.get("SHOPIFY_API_VERSION") or "2026-04").strip()


_GID_NUMERIC = re.compile(r"^gid://shopify/Customer/(\d+)$")
_GID_COMPANY = re.compile(r"^gid://shopify/Company/(\d+)$")


def _customer_gid_to_numeric_id(gid: str) -> str:
    """Convierte gid://shopify/Customer/123 → 123 para persistir en `clients.shopify_customer_id`."""
    m = _GID_NUMERIC.match(gid.strip())
    if m:
        return m.group(1)
    if gid.isdigit():
        return gid
    raise RuntimeError(f"id de cliente Shopify inesperado: {gid!r}")


def _company_gid_to_numeric_id(gid: str) -> str:
    """Convierte gid://shopify/Company/123 → 123 para persistir en `companies.shopify_company_id`."""
    m = _GID_COMPANY.match(gid.strip())
    if m:
        return m.group(1)
    if gid.strip().isdigit():
        return gid.strip()
    raise RuntimeError(f"id de company Shopify inesperado: {gid!r}")


def _to_customer_gid(numeric_or_gid: str) -> str:
    s = numeric_or_gid.strip()
    if s.startswith("gid://shopify/Customer/"):
        return s
    if s.isdigit():
        return f"gid://shopify/Customer/{s}"
    raise ValueError(f"customer id inválido para Shopify: {numeric_or_gid!r}")


def _email_search_syntax(email: str) -> str:
    """Sintaxis de búsqueda Admin API: email:\"...\""""
    esc = email.replace("\\", "\\\\").replace('"', '\\"')
    return f'email:"{esc}"'


class ShopifyAPIError(Exception):
    """Error HTTP o error GraphQL al llamar a Admin API."""

    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"Shopify API {status}: {payload}")


def format_shopify_api_error_detail(exc: ShopifyAPIError, *, max_len: int = 4000) -> str:
    """Resume `exc.payload` para mostrar al usuario o en logs (userErrors, graphql_errors, etc.)."""
    p = exc.payload
    if not isinstance(p, dict):
        s = str(p)
        return s if len(s) <= max_len else s[: max_len - 3] + "..."

    chunks: list[str] = []

    def append_user_errors(label: str, items: Any) -> None:
        if not isinstance(items, list) or not items:
            return
        parts: list[str] = []
        for item in items:
            if isinstance(item, dict):
                msg = item.get("message") or str(item)
                bits = [msg]
                if item.get("code"):
                    bits.append(f"code={item['code']}")
                if item.get("field"):
                    bits.append(f"field={item['field']}")
                parts.append(" — ".join(bits))
            else:
                parts.append(str(item))
        if parts:
            chunks.append(f"{label}: " + " | ".join(parts))

    append_user_errors("userErrors", p.get("userErrors"))

    for key in (
        "graphql_errors",
        "errors",
    ):
        raw_list = p.get(key)
        if isinstance(raw_list, list) and raw_list:
            msgs: list[str] = []
            for g in raw_list:
                if isinstance(g, dict):
                    msgs.append(
                        str(g.get("message") or g.get("extensions") or g)
                    )
                else:
                    msgs.append(str(g))
            chunks.append(f"{key}: " + " | ".join(msgs))

    for nested_key in (
        "companyCreate",
        "companyAssignCustomerAsContact",
    ):
        nested = p.get(nested_key)
        if isinstance(nested, dict):
            append_user_errors(nested_key, nested.get("userErrors"))

    if p.get("message") and isinstance(p["message"], str):
        chunks.append(p["message"])

    if p.get("raw") and isinstance(p["raw"], str):
        chunks.append(p["raw"][:500])

    if not chunks:
        fallback = json.dumps(p, ensure_ascii=False, default=str)
        if len(fallback) > max_len:
            fallback = fallback[: max_len - 3] + "..."
        return fallback

    out = " ".join(chunks)
    if len(out) > max_len:
        return out[: max_len - 3] + "..."
    return out


def _graphql_request(
    shop_domain: str,
    access_token: str,
    query: str,
    variables: dict[str, Any] | None,
) -> dict[str, Any]:
    url = f"https://{shop_domain}/admin/api/{_api_version()}/graphql.json"
    body: dict[str, Any] = {
        "query": query,
        "variables": variables if variables is not None else {},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        raise ShopifyAPIError(e.code, parsed) from e

    try:
        parsed: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ShopifyAPIError(code, {"raw": raw}) from e

    gerrs = parsed.get("errors")
    data = parsed.get("data")
    if gerrs and data is None:
        raise ShopifyAPIError(200, {"graphql_errors": gerrs})

    if data is None:
        raise ShopifyAPIError(code, parsed)
    return data


def find_customer_numeric_id_by_email(
    shop_domain: str,
    access_token: str,
    email: str,
) -> str | None:
    if not email:
        return None
    try:
        data = _graphql_request(
            shop_domain,
            access_token,
            QUERY_CUSTOMERS,
            {"first": 5, "query": _email_search_syntax(email.strip())},
        )
    except ShopifyAPIError:
        return None
    edges = (data.get("customers") or {}).get("edges") or []
    if not edges:
        return None
    gid = (edges[0].get("node") or {}).get("id")
    if not gid:
        return None
    return _customer_gid_to_numeric_id(gid)


def create_customer(
    shop_domain: str,
    access_token: str,
    *,
    email: str | None,
    first_name: str | None,
    last_name: str | None,
    phone: str | None,
) -> str:
    """Crea un cliente con `customerCreate`; si hay userErrors (p. ej. email duplicado), busca por email."""
    input_payload: dict[str, Any] = {}
    if email:
        input_payload["email"] = email
    fn = (first_name or "").strip()
    ln = (last_name or "").strip()
    if fn:
        input_payload["firstName"] = fn
    if ln:
        input_payload["lastName"] = ln
    ph = (phone or "").strip()
    if ph:
        input_payload["phone"] = ph

    try:
        data = _graphql_request(
            shop_domain,
            access_token,
            MUTATION_CUSTOMER_CREATE,
            {"input": input_payload},
        )
    except ShopifyAPIError:
        raise

    cc = data.get("customerCreate") or {}
    user_errors = cc.get("userErrors") or []
    cust = cc.get("customer")
    if cust and cust.get("id"):
        return _customer_gid_to_numeric_id(cust["id"])

    if user_errors and email:
        existing = find_customer_numeric_id_by_email(
            shop_domain, access_token, email
        )
        if existing:
            return existing

    raise ShopifyAPIError(
        422,
        {"userErrors": user_errors, "message": "customerCreate sin customer"},
    )


def _omit_none_nested(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, dict):
            inner = _omit_none_nested(v)
            if inner:
                out[k] = inner
        else:
            out[k] = v
    return out


def build_company_location_input(
    company_name: str,
    *,
    shipping_first_name: str | None,
    shipping_last_name: str | None,
    contact_phone: str | None,
    shipping: dict[str, Any],
    location_name: str | None = None,
) -> dict[str, Any]:
    """`shipping` incluye address1, city, country_code (ISO-2); zip y address2 opcionales.

    Si ``location_name`` se omite, usa ``"<company_name> — Principal"``.
    """
    fn = (shipping_first_name or "").strip() or "—"
    ln = (shipping_last_name or "").strip() or "—"
    zip_val = str(shipping.get("zip") or "").strip()
    addr: dict[str, Any] = {
        "firstName": fn,
        "lastName": ln,
        "address1": shipping["address1"].strip(),
        "city": shipping["city"].strip(),
        "countryCode": shipping["country_code"].strip().upper(),
    }
    if zip_val:
        addr["zip"] = zip_val
    if shipping.get("address2"):
        addr["address2"] = str(shipping["address2"]).strip()
    if shipping.get("zone_code"):
        addr["zoneCode"] = str(shipping["zone_code"]).strip()
    ph = (contact_phone or "").strip()
    if ph:
        addr["phone"] = ph

    name = (location_name or "").strip() or f"{company_name.strip()} — Principal"
    loc: dict[str, Any] = {
        "name": name,
        "shippingAddress": addr,
        "billingSameAsShipping": True,
    }
    if ph:
        loc["phone"] = ph
    return loc


# Compat: nombre privado anterior usado dentro de este módulo.
_build_company_location_input = build_company_location_input


def _build_company_address_input(
    shipping: dict[str, Any],
    *,
    first_name: str | None,
    last_name: str | None,
    phone: str | None,
) -> dict[str, Any]:
    """Construye un ``CompanyAddressInput`` (sin name) para companyLocationAssignAddress."""
    fn = (first_name or "").strip() or "—"
    ln = (last_name or "").strip() or "—"
    addr: dict[str, Any] = {
        "firstName": fn,
        "lastName": ln,
        "address1": shipping["address1"].strip(),
        "city": shipping["city"].strip(),
        "countryCode": shipping["country_code"].strip().upper(),
    }
    zip_val = str(shipping.get("zip") or "").strip()
    if zip_val:
        addr["zip"] = zip_val
    if shipping.get("address2"):
        addr["address2"] = str(shipping["address2"]).strip()
    if shipping.get("zone_code"):
        addr["zoneCode"] = str(shipping["zone_code"]).strip()
    ph = (phone or "").strip()
    if ph:
        addr["phone"] = ph
    return addr


_MUTATION_COMPANY_LOCATION_CREATE = """
mutation CompanyLocationCreate($companyId: ID!, $input: CompanyLocationInput!) {
  companyLocationCreate(companyId: $companyId, input: $input) {
    companyLocation {
      id
      name
    }
    userErrors { field message code }
  }
}
"""

_MUTATION_COMPANY_LOCATION_UPDATE = """
mutation CompanyLocationUpdate($companyLocationId: ID!, $input: CompanyLocationUpdateInput!) {
  companyLocationUpdate(companyLocationId: $companyLocationId, input: $input) {
    companyLocation { id name phone }
    userErrors { field message code }
  }
}
"""

_MUTATION_COMPANY_LOCATION_ASSIGN_ADDRESS = """
mutation CompanyLocationAssignAddress($locationId: ID!, $address: CompanyAddressInput!, $addressTypes: [CompanyAddressType!]!) {
  companyLocationAssignAddress(locationId: $locationId, address: $address, addressTypes: $addressTypes) {
    addresses { id address1 city zip countryCode }
    userErrors { field message code }
  }
}
"""

_MUTATION_COMPANY_LOCATION_DELETE = """
mutation CompanyLocationDelete($companyLocationId: ID!) {
  companyLocationDelete(companyLocationId: $companyLocationId) {
    deletedCompanyLocationId
    userErrors { field message code }
  }
}
"""


def _to_company_gid(company_id: str) -> str:
    s = str(company_id).strip()
    if s.startswith("gid://shopify/Company/"):
        return s
    if s.isdigit():
        return f"gid://shopify/Company/{s}"
    raise ValueError(f"company id inválido para Shopify: {company_id!r}")


def create_company_location(
    shop_domain: str,
    access_token: str,
    *,
    shopify_company_id: str,
    company_name: str,
    shipping: dict[str, Any],
    location_name: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    phone: str | None = None,
) -> str:
    """Crea una CompanyLocation en una empresa B2B existente y devuelve su GID.

    ``shopify_company_id`` puede ser un GID o el id numérico. ``shipping`` usa el
    mismo shape normalizado que ``shipping_for_shopify_b2b`` (address1, city,
    country_code obligatorios; zip, address2, zone_code opcionales).

    Lanza ``ShopifyAPIError`` si Shopify devuelve userErrors.
    """
    location_input = build_company_location_input(
        company_name,
        shipping_first_name=first_name,
        shipping_last_name=last_name,
        contact_phone=phone,
        shipping=shipping,
        location_name=location_name,
    )
    data = _graphql_request(
        shop_domain,
        access_token,
        _MUTATION_COMPANY_LOCATION_CREATE,
        {
            "companyId": _to_company_gid(shopify_company_id),
            "input": location_input,
        },
    )
    payload = data.get("companyLocationCreate") or {}
    errs = payload.get("userErrors") or []
    loc = payload.get("companyLocation") or {}
    location_gid = loc.get("id")
    if errs or not location_gid:
        raise ShopifyAPIError(
            422, {"userErrors": errs, "message": "companyLocationCreate"}
        )
    return str(location_gid)


def update_company_location(
    shop_domain: str,
    access_token: str,
    *,
    location_gid: str,
    name: str | None = None,
    phone: str | None = None,
    shipping: dict[str, Any] | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> None:
    """Actualiza una CompanyLocation existente.

    - ``name``/``phone`` se envían vía ``companyLocationUpdate``.
    - Si ``shipping`` se incluye, se asigna como SHIPPING + BILLING vía
      ``companyLocationAssignAddress`` (mantiene ``billingSameAsShipping``).

    Lanza ``ShopifyAPIError`` si Shopify devuelve userErrors.
    """
    update_input: dict[str, Any] = {}
    if name is not None:
        nm = name.strip()
        if nm:
            update_input["name"] = nm
    if phone is not None:
        ph = phone.strip()
        update_input["phone"] = ph if ph else None

    if update_input:
        data = _graphql_request(
            shop_domain,
            access_token,
            _MUTATION_COMPANY_LOCATION_UPDATE,
            {"companyLocationId": location_gid, "input": update_input},
        )
        payload = data.get("companyLocationUpdate") or {}
        errs = payload.get("userErrors") or []
        if errs:
            raise ShopifyAPIError(
                422, {"userErrors": errs, "message": "companyLocationUpdate"}
            )

    if shipping is not None:
        address_input = _build_company_address_input(
            shipping,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
        )
        data = _graphql_request(
            shop_domain,
            access_token,
            _MUTATION_COMPANY_LOCATION_ASSIGN_ADDRESS,
            {
                "locationId": location_gid,
                "address": address_input,
                "addressTypes": ["SHIPPING", "BILLING"],
            },
        )
        payload = data.get("companyLocationAssignAddress") or {}
        errs = payload.get("userErrors") or []
        if errs:
            raise ShopifyAPIError(
                422,
                {"userErrors": errs, "message": "companyLocationAssignAddress"},
            )


def delete_company_location(
    shop_domain: str,
    access_token: str,
    location_gid: str,
) -> None:
    """Elimina una CompanyLocation en Shopify B2B.

    Lanza ``ShopifyAPIError`` si Shopify devuelve userErrors.
    """
    data = _graphql_request(
        shop_domain,
        access_token,
        _MUTATION_COMPANY_LOCATION_DELETE,
        {"companyLocationId": location_gid},
    )
    payload = data.get("companyLocationDelete") or {}
    errs = payload.get("userErrors") or []
    if errs:
        raise ShopifyAPIError(
            422, {"userErrors": errs, "message": "companyLocationDelete"}
        )


def _company_user_errors(data: dict[str, Any]) -> list[dict[str, Any]]:
    return list((data.get("companyCreate") or {}).get("userErrors") or [])


def _assign_user_errors(data: dict[str, Any]) -> list[dict[str, Any]]:
    return list(
        (data.get("companyAssignCustomerAsContact") or {}).get("userErrors") or []
    )


_CATALOG_CONTEXT_UPDATE = """
mutation CatalogContextUpdate(
  $catalogId: ID!,
  $contextsToAdd: CatalogContextInput,
  $contextsToRemove: CatalogContextInput
) {
  catalogContextUpdate(
    catalogId: $catalogId,
    contextsToAdd: $contextsToAdd,
    contextsToRemove: $contextsToRemove
  ) {
    catalog { id title status }
    userErrors { field message code }
  }
}
"""


def _extract_location_gids(comp: dict[str, Any]) -> list[str]:
    """Extrae los GIDs de CompanyLocation del dict `company` devuelto por companyCreate."""
    edges = (comp.get("locations") or {}).get("edges") or []
    out: list[str] = []
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        gid = node.get("id") if isinstance(node, dict) else None
        if isinstance(gid, str) and gid:
            out.append(gid)
    return out


def _shopify_b2b_ids(
    customer_numeric: str,
    company_gid: str,
    location_gids: list[str] | None = None,
) -> dict[str, Any]:
    cg = str(company_gid)
    return {
        "shopify_customer_numeric_id": customer_numeric,
        "shopify_company_gid": cg,
        "shopify_company_numeric_id": _company_gid_to_numeric_id(cg),
        "shopify_company_location_gids": location_gids or [],
    }


def add_locations_to_catalog(
    shop_domain: str,
    access_token: str,
    catalog_id: str,
    location_gids: list[str],
) -> None:
    """Agrega CompanyLocations a un Catalog B2B existente en Shopify.

    Lanza ``ShopifyAPIError`` si Shopify devuelve userErrors.
    """
    data = _graphql_request(
        shop_domain,
        access_token,
        _CATALOG_CONTEXT_UPDATE,
        {
            "catalogId": catalog_id,
            "contextsToAdd": {"companyLocationIds": location_gids},
            "contextsToRemove": None,
        },
    )
    payload = data.get("catalogContextUpdate") or {}
    errs = payload.get("userErrors") or []
    if errs:
        raise ShopifyAPIError(422, {"userErrors": errs, "message": "catalogContextUpdate"})


_QUERY_COMPANY_MAIN_CONTACT = """
query CompanyMainContact($id: ID!) {
  company(id: $id) {
    id
    name
    mainContact {
      id
      customer {
        id
        defaultEmailAddress { emailAddress }
      }
    }
    contacts(first: 20) {
      nodes {
        id
        customer {
          id
          defaultEmailAddress { emailAddress }
        }
      }
    }
  }
}
"""

_MUTATION_COMPANY_ASSIGN_MAIN_CONTACT = """
mutation CompanyAssignMainContact($companyId: ID!, $companyContactId: ID!) {
  companyAssignMainContact(companyId: $companyId, companyContactId: $companyContactId) {
    company {
      id
      mainContact {
        id
        customer { id }
      }
    }
    userErrors { field message code }
  }
}
"""


def get_company_contact_info(
    shop_domain: str,
    access_token: str,
    company_gid: str,
) -> dict[str, Any] | None:
    """Devuelve ``{mainContact, contacts}`` de la company, o None si Shopify no la encuentra.

    ``mainContact`` es None si no hay contacto principal asignado.
    ``contacts`` es la lista de CompanyContact con su customer GID.
    Lanza ``ShopifyAPIError`` si la llamada a Shopify falla.
    """
    data = _graphql_request(
        shop_domain,
        access_token,
        _QUERY_COMPANY_MAIN_CONTACT,
        {"id": company_gid},
    )
    node = data.get("company")
    if not isinstance(node, dict):
        return None
    contacts_nodes = (node.get("contacts") or {}).get("nodes") or []
    return {
        "mainContact": node.get("mainContact") or None,
        "contacts": contacts_nodes,
    }


def get_company_main_contact(
    shop_domain: str,
    access_token: str,
    company_gid: str,
) -> dict[str, Any] | None:
    """Devuelve el dict de mainContact de la company o None si no tiene.

    Lanza ``ShopifyAPIError`` si la llamada a Shopify falla.
    Devuelve None si Shopify no devuelve la company (borrada o ID inválido).
    """
    info = get_company_contact_info(shop_domain, access_token, company_gid)
    if info is None:
        return None
    return info["mainContact"]


def set_main_contact(
    shop_domain: str,
    access_token: str,
    company_gid: str,
    company_contact_gid: str,
) -> None:
    """Designa un CompanyContact ya existente como mainContact de la company.

    Lanza ``ShopifyAPIError`` si Shopify devuelve userErrors.
    """
    data = _graphql_request(
        shop_domain,
        access_token,
        _MUTATION_COMPANY_ASSIGN_MAIN_CONTACT,
        {"companyId": company_gid, "companyContactId": company_contact_gid},
    )
    payload = data.get("companyAssignMainContact") or {}
    errs = payload.get("userErrors") or []
    if errs:
        raise ShopifyAPIError(422, {"userErrors": errs, "message": "companyAssignMainContact"})


def assign_customer_as_contact(
    shop_domain: str,
    access_token: str,
    company_gid: str,
    customer_numeric_id: str,
) -> None:
    """Asigna un Customer existente como CompanyContact de la company.

    Lanza ``ShopifyAPIError`` si Shopify devuelve userErrors.
    """
    data = _graphql_request(
        shop_domain,
        access_token,
        MUTATION_COMPANY_ASSIGN_CUSTOMER,
        {
            "companyId": company_gid,
            "customerId": _to_customer_gid(customer_numeric_id),
        },
    )
    payload = data.get("companyAssignCustomerAsContact") or {}
    errs = payload.get("userErrors") or []
    if errs:
        raise ShopifyAPIError(422, {"userErrors": errs, "message": "companyAssignCustomerAsContact"})


def ensure_shopify_b2b_company(
    shop_domain: str,
    access_token: str,
    *,
    external_id: str,
    company_name: str,
    contact_email: str,
    first_name: str | None,
    last_name: str | None,
    contact_phone: str | None,
    shipping: dict[str, Any],
) -> dict[str, str]:
    """Crea la empresa B2B en Shopify y devuelve ids del cliente principal y de la company.

    - Si ya existe un **Customer** con el mismo email, crea la company **sin** `companyContact`
      y enlaza con `companyAssignCustomerAsContact`.
    - Si no existe, usa `companyCreate` con `companyContact` + `companyLocation` (Shopify crea el cliente).

    `shipping` debe incluir al menos: address1, city, country_code (ISO-2). `zip` es opcional.
    """
    email = (contact_email or "").strip()
    if not email:
        raise ValueError("contact_email es obligatorio para Shopify B2B")

    existing_numeric = find_customer_numeric_id_by_email(
        shop_domain, access_token, email
    )

    company_block: dict[str, Any] = {
        "name": company_name.strip(),
        "externalId": external_id.strip(),
    }
    location_block = _build_company_location_input(
        company_name,
        shipping_first_name=shipping.get("first_name") or first_name,
        shipping_last_name=shipping.get("last_name") or last_name,
        contact_phone=contact_phone,
        shipping=shipping,
    )

    if existing_numeric:
        create_input: dict[str, Any] = {
            "company": company_block,
            "companyLocation": location_block,
        }
        data = _graphql_request(
            shop_domain,
            access_token,
            MUTATION_COMPANY_CREATE,
            {"input": _omit_none_nested(create_input)},
        )
        errs = _company_user_errors(data)
        comp = (data.get("companyCreate") or {}).get("company") or {}
        company_gid = comp.get("id")
        if errs or not company_gid:
            raise ShopifyAPIError(
                422,
                {"userErrors": errs, "message": "companyCreate (cliente existente)"},
            )

        assign_data = _graphql_request(
            shop_domain,
            access_token,
            MUTATION_COMPANY_ASSIGN_CUSTOMER,
            {
                "companyId": company_gid,
                "customerId": _to_customer_gid(existing_numeric),
            },
        )
        aerrs = _assign_user_errors(assign_data)
        if aerrs:
            raise ShopifyAPIError(
                422,
                {"userErrors": aerrs, "message": "companyAssignCustomerAsContact"},
            )

        # companyAssignCustomerAsContact crea el contacto pero NO lo marca como mainContact.
        # Hay que promoverlo explícitamente para que el cliente pueda comprar como B2B.
        contact_gid = (
            (assign_data.get("companyAssignCustomerAsContact") or {})
            .get("companyContact", {})
            .get("id")
        )
        if contact_gid:
            set_main_contact(shop_domain, access_token, str(company_gid), contact_gid)

        return _shopify_b2b_ids(
            existing_numeric, str(company_gid), _extract_location_gids(comp)
        )

    fn = (first_name or "").strip()
    ln = (last_name or "").strip()
    contact_block: dict[str, Any] = {
        "email": email,
        "firstName": fn or "—",
        "lastName": ln or "—",
    }
    ph_c = (contact_phone or "").strip()
    if ph_c:
        contact_block["phone"] = ph_c
    create_input = {
        "company": company_block,
        "companyContact": contact_block,
        "companyLocation": location_block,
    }
    data = _graphql_request(
        shop_domain,
        access_token,
        MUTATION_COMPANY_CREATE,
        {"input": _omit_none_nested(create_input)},
    )
    errs = _company_user_errors(data)
    comp = (data.get("companyCreate") or {}).get("company") or {}
    company_gid = comp.get("id")
    main = comp.get("mainContact") or {}
    cust = main.get("customer") or {}
    cust_gid = cust.get("id")

    if errs:
        raise ShopifyAPIError(
            422,
            {"userErrors": errs, "message": "companyCreate (cliente nuevo)"},
        )
    if not company_gid:
        raise ShopifyAPIError(
            422,
            {"message": "companyCreate no devolvió company.id"},
        )
    loc_gids = _extract_location_gids(comp)
    if cust_gid:
        return _shopify_b2b_ids(
            _customer_gid_to_numeric_id(str(cust_gid)),
            str(company_gid),
            loc_gids,
        )

    # Carrera: el cliente pudo crearse fuera de mainContact; reintento con assign.
    existing2 = find_customer_numeric_id_by_email(
        shop_domain, access_token, email
    )
    if existing2:
        assign_data = _graphql_request(
            shop_domain,
            access_token,
            MUTATION_COMPANY_ASSIGN_CUSTOMER,
            {
                "companyId": company_gid,
                "customerId": _to_customer_gid(existing2),
            },
        )
        aerrs = _assign_user_errors(assign_data)
        if aerrs:
            raise ShopifyAPIError(
                422,
                {"userErrors": aerrs, "message": "companyAssignCustomerAsContact (fallback)"},
            )
        contact_gid = (
            (assign_data.get("companyAssignCustomerAsContact") or {})
            .get("companyContact", {})
            .get("id")
        )
        if contact_gid:
            set_main_contact(shop_domain, access_token, str(company_gid), contact_gid)
        return _shopify_b2b_ids(existing2, str(company_gid), loc_gids)

    raise ShopifyAPIError(
        422,
        {
            "message": "companyCreate sin mainContact.customer; no se encontró cliente por email",
            "company_gid": company_gid,
        },
    )
