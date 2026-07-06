"""Arma las mutaciones de Draft Order para las cotizaciones negociadas.

Reusa el cliente Admin GraphQL compartido (``shopify/graphql.py``), la
resolución de tienda/token (``shopify/store.py``) y de CompanyLocation
(``shopify/companies.py``). Las mutaciones fueron validadas con el MCP
shopify-dev (``validate_graphql_codeblocks``).

Convención de impuestos: ``negotiated_unit_price`` es **neto**; se pasa como
``priceOverride`` y Shopify aplica el impuesto según la config de la tienda /
CompanyLocation. Verificar la coherencia del ``totalPriceSet`` vs el PDF.
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path
from urllib.request import Request, urlopen

from aws_lambda_powertools import Logger
from sqlalchemy.orm import Session

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

from database.models.quote_request import QuoteRequest  # noqa: E402
from shopify.companies import (  # noqa: E402
    first_location_for_company,
    main_contact_for_company,
    normalize_company_gid,
)
from shopify.graphql import format_user_errors, graphql_call, shopify_api_version  # noqa: E402
from shopify.store import resolve_store_token  # noqa: E402

logger = Logger()

_DRAFT_ORDER_CREATE = """
mutation crearCotizacionNegociada($input: DraftOrderInput!) {
  draftOrderCreate(input: $input) {
    draftOrder {
      id
      name
      ready
      status
      totalPriceSet { shopMoney { amount currencyCode } }
      purchasingEntity {
        ... on PurchasingCompany {
          company { id name }
          location { id name }
        }
      }
    }
    userErrors { field message }
  }
}
"""

_DRAFT_ORDER_UPDATE = """
mutation actualizarCotizacion($id: ID!, $input: DraftOrderInput!) {
  draftOrderUpdate(id: $id, input: $input) {
    draftOrder {
      id
      name
      status
      totalPriceSet { shopMoney { amount currencyCode } }
    }
    userErrors { field message }
  }
}
"""

_DRAFT_ORDER_INVOICE_SEND = """
mutation enviarInvoice($id: ID!) {
  draftOrderInvoiceSend(id: $id) {
    draftOrder { id status invoiceUrl }
    userErrors { field message }
  }
}
"""

def fetch_rates_from_shipping_service(
    city: str, auth_header: str, packages: list[dict] | None = None
) -> list[dict]:
    """Llama al servicio shipping propio para obtener la tarifa de despacho a una ciudad.

    Usa el mismo token Bearer del usuario que ya autenticó el request de cotizaciones,
    evitando la necesidad de credenciales adicionales.
    ``packages`` es una lista de dicts con ``weight_kg``; si está vacía el servicio
    asume 1 kg por defecto.
    """
    import os
    shipping_url = (os.environ.get("SHIPPING_API_URL") or "").rstrip("/")
    if not shipping_url or not auth_header or not city:
        return []
    payload = json.dumps({"destination_locality": city, "packages": packages or []}).encode("utf-8")
    req = Request(
        f"{shipping_url}/api/v1/shipping/quote",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": auth_header,
        },
    )
    try:
        with urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        result = (body.get("data") or {})
        # Usar precio neto (sin IVA) para que el modal lo muestre correctamente
        total_neto = result.get("total_neto_clp")
        if total_neto is None:
            return []
        title = "Apro Click — Carga consolidada"
        return [{"title": title, "handle": "shipping-service", "price": str(int(total_neto)), "currency": "CLP"}]
    except Exception as e:
        logger.warning("Llamada al shipping service falló", city=city, error=str(e))
        return []

_DELIVERY_PROFILES = """
query tarifasDespacho {
  deliveryProfiles(first: 20) {
    edges {
      node {
        profileLocationGroups {
          locationGroupZones(first: 20) {
            edges {
              node {
                methodDefinitions(first: 30) {
                  edges {
                    node {
                      name
                      active
                      rateProvider {
                        ... on DeliveryRateDefinition {
                          price { amount currencyCode }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

_LOCATION_ADDRESS = """
query direccionLocation($id: ID!) {
  companyLocation(id: $id) {
    name
    billingAddress {
      address1
      address2
      city
      zoneCode
      zip
      countryCode
      phone
    }
  }
}
"""

_VARIANT_WEIGHTS = """
query pesoVariantes($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      weight
      weightUnit
    }
  }
}
"""

_VARIANT_BY_SKU = """
query variantePorSKU($query: String!) {
  productVariants(first: 5, query: $query) {
    edges {
      node {
        id
        sku
        displayName
        price
        product { title }
      }
    }
  }
}
"""

_DRAFT_ORDER_COMPLETE = """
mutation completarDraft($id: ID!) {
  draftOrderComplete(id: $id) {
    draftOrder { id status order { id name } }
    userErrors { field message }
  }
}
"""


def _money(value: Decimal | None, currency: str) -> dict | None:
    if value is None:
        return None
    return {"amount": str(value), "currencyCode": currency or "CLP"}


def resolve_variant_by_sku(shop_domain: str, token: str, sku: str) -> dict | None:
    """Devuelve el nodo ProductVariant de Shopify que coincide exactamente con el SKU, o None."""
    if not sku:
        return None
    try:
        data = graphql_call(shop_domain, token, _VARIANT_BY_SKU, {"query": f"sku:{sku}"})
        edges = (data.get("productVariants") or {}).get("edges") or []
        sku_lower = sku.strip().lower()
        for edge in edges:
            node = (edge or {}).get("node") or {}
            if (node.get("sku") or "").strip().lower() == sku_lower:
                return node
        return None
    except Exception as e:
        logger.warning("No se pudo resolver variant por SKU", sku=sku, error=str(e))
        return None


def _fetch_location_mailing_address(
    shop_domain: str, token: str, location_gid: str
) -> dict | None:
    """Devuelve un MailingAddressInput con la dirección de la CompanyLocation, o None si falla."""
    try:
        data = graphql_call(shop_domain, token, _LOCATION_ADDRESS, {"id": location_gid})
        loc = (data.get("companyLocation") or {})
        addr = loc.get("billingAddress") or {}
        if not addr.get("address1") and not addr.get("city"):
            return None
        name = loc.get("name") or ""
        mailing: dict = {
            "firstName": name,
            "lastName": "",
            "address1": addr.get("address1") or "",
            "address2": addr.get("address2") or "",
            "city": addr.get("city") or "",
            "province": addr.get("zoneCode") or "",
            "zip": addr.get("zip") or "",
            "country": addr.get("countryCode") or "CL",
            "phone": addr.get("phone") or "",
        }
        return mailing
    except Exception as e:
        logger.warning("No se pudo obtener dirección de location", location=location_gid, error=str(e))
        return None


_WEIGHT_TO_KG: dict[str, float] = {
    "KILOGRAMS": 1.0,
    "GRAMS": 0.001,
    "POUNDS": 0.453592,
    "OUNCES": 0.0283495,
}


def _fetch_variant_weights(
    shop_domain: str, token: str, variant_gids: list[str]
) -> dict[str, float]:
    """Devuelve un mapa variant_gid → peso en kg.  Omite variants con peso 0 o desconocido."""
    if not variant_gids:
        return {}
    try:
        data = graphql_call(shop_domain, token, _VARIANT_WEIGHTS, {"ids": variant_gids})
        result: dict[str, float] = {}
        for node in (data.get("nodes") or []):
            if not node or "weight" not in node:
                continue
            gid = node.get("id")
            raw_weight = node.get("weight") or 0
            unit = (node.get("weightUnit") or "KILOGRAMS").upper()
            kg = float(raw_weight) * _WEIGHT_TO_KG.get(unit, 1.0)
            if gid and kg > 0:
                result[gid] = kg
        return result
    except Exception as e:
        logger.warning("No se pudieron obtener pesos de variants", error=str(e))
        return {}


def _build_draft_input(
    quote: QuoteRequest, location_gid: str, contact_gid: str,
) -> dict:
    company_gid = normalize_company_gid(quote.shopify_company_id or "")
    line_items: list[dict] = []
    for ln in sorted(quote.lines, key=lambda x: x.line_number):
        item: dict = {
            "variantId": ln.variant_gid,
            "quantity": int(ln.quantity or 1),
        }
        price = ln.negotiated_unit_price
        if price is not None:
            item["priceOverride"] = _money(price, ln.currency)
        if isinstance(ln.applied_discount, dict) and ln.applied_discount:
            item["appliedDiscount"] = ln.applied_discount
        line_items.append(item)

    draft_input: dict = {
        "purchasingEntity": {
            "purchasingCompany": {
                "companyId": company_gid,
                "companyContactId": contact_gid,
                "companyLocationId": location_gid,
            }
        },
        "lineItems": line_items,
    }
    if isinstance(quote.order_applied_discount, dict) and quote.order_applied_discount:
        draft_input["appliedDiscount"] = quote.order_applied_discount
    if isinstance(quote.shipping_line, dict) and quote.shipping_line:
        sl = quote.shipping_line
        price_val = sl.get("price")
        title = sl.get("title")
        if title and price_val is not None:
            try:
                # shippingLine.price es el escalar Money (string decimal), no MoneyInput
                draft_input["shippingLine"] = {
                    "price": str(Decimal(str(price_val))),
                    "title": str(title),
                }
            except Exception:
                pass
    return draft_input


def _build_update_input(quote: QuoteRequest) -> dict:
    """Input para draftOrderUpdate (sin purchasingEntity, que no se puede cambiar)."""
    line_items: list[dict] = []
    for ln in sorted(quote.lines, key=lambda x: x.line_number):
        item: dict = {
            "variantId": ln.variant_gid,
            "quantity": int(ln.quantity or 1),
        }
        price = ln.negotiated_unit_price
        if price is not None:
            item["priceOverride"] = _money(price, ln.currency)
        if isinstance(ln.applied_discount, dict) and ln.applied_discount:
            item["appliedDiscount"] = ln.applied_discount
        line_items.append(item)

    update_input: dict = {"lineItems": line_items}
    if isinstance(quote.order_applied_discount, dict) and quote.order_applied_discount:
        update_input["appliedDiscount"] = quote.order_applied_discount
    if isinstance(quote.shipping_line, dict) and quote.shipping_line:
        sl = quote.shipping_line
        price_val = sl.get("price")
        title = sl.get("title")
        if title and price_val is not None:
            try:
                # shippingLine.price es el escalar Money (string decimal), no MoneyInput
                update_input["shippingLine"] = {
                    "price": str(Decimal(str(price_val))),
                    "title": str(title),
                }
            except Exception:
                pass
    return update_input


def update_quote_draft(session: Session, quote: QuoteRequest) -> dict:
    """Actualiza el Draft Order existente con precios/envío/líneas modificados por SALES."""
    if not quote.draft_order_gid:
        raise ValueError("La cotización no tiene Draft Order asociado")
    shop_domain, token = resolve_store_token(session, quote.shop_domain)
    update_input = _build_update_input(quote)
    data = graphql_call(
        shop_domain, token, _DRAFT_ORDER_UPDATE,
        {"id": quote.draft_order_gid, "input": update_input},
    )
    result = data.get("draftOrderUpdate") or {}
    errs = result.get("userErrors") or []
    if errs:
        raise ValueError(f"draftOrderUpdate: {format_user_errors(errs)}")
    return result.get("draftOrder") or {}


def create_quote_draft(session: Session, quote: QuoteRequest) -> dict:
    """Crea el Draft Order en Shopify a partir de la cotización negociada.

    Devuelve el ``draftOrder`` (dict). Persiste ``draft_order_gid`` en la quote.
    Lanza ``ValueError`` si Shopify devuelve ``userErrors`` o falta la location.
    """
    shop_domain, token = resolve_store_token(session, quote.shop_domain)

    location_gid = (quote.company_location_gid or "").strip()
    if not location_gid:
        location_gid = first_location_for_company(
            shop_domain, token, quote.shopify_company_id or ""
        ) or ""
    if not location_gid:
        raise ValueError(
            "No se pudo resolver la CompanyLocation de la empresa en Shopify"
        )
    quote.company_location_gid = location_gid

    contact_gid = main_contact_for_company(
        shop_domain, token, quote.shopify_company_id or ""
    )
    if not contact_gid:
        raise ValueError(
            "La empresa no tiene un contacto (CompanyContact) en Shopify; "
            "Shopify lo exige para crear el pedido B2B"
        )

    draft_input = _build_draft_input(quote, location_gid, contact_gid)
    data = graphql_call(shop_domain, token, _DRAFT_ORDER_CREATE, {"input": draft_input})
    result = (data.get("draftOrderCreate") or {})
    errs = result.get("userErrors") or []
    if errs:
        raise ValueError(f"draftOrderCreate: {format_user_errors(errs)}")
    draft = result.get("draftOrder") or {}
    gid = draft.get("id")
    if not gid:
        raise ValueError("draftOrderCreate no devolvió draftOrder.id")
    quote.draft_order_gid = gid
    return draft


def send_quote_invoice(session: Session, quote: QuoteRequest) -> dict:
    """Envía la invoice (link de checkout) del Draft Order al cliente."""
    if not quote.draft_order_gid:
        raise ValueError("La cotización no tiene Draft Order asociado")
    shop_domain, token = resolve_store_token(session, quote.shop_domain)
    data = graphql_call(
        shop_domain, token, _DRAFT_ORDER_INVOICE_SEND, {"id": quote.draft_order_gid}
    )
    result = data.get("draftOrderInvoiceSend") or {}
    errs = result.get("userErrors") or []
    if errs:
        raise ValueError(f"draftOrderInvoiceSend: {format_user_errors(errs)}")
    return result.get("draftOrder") or {}


def complete_quote(session: Session, quote: QuoteRequest) -> dict:
    """Completa el Draft Order (net terms) → genera la Order en Shopify."""
    if not quote.draft_order_gid:
        raise ValueError("La cotización no tiene Draft Order asociado")
    shop_domain, token = resolve_store_token(session, quote.shop_domain)
    data = graphql_call(
        shop_domain, token, _DRAFT_ORDER_COMPLETE, {"id": quote.draft_order_gid}
    )
    result = data.get("draftOrderComplete") or {}
    errs = result.get("userErrors") or []
    if errs:
        raise ValueError(f"draftOrderComplete: {format_user_errors(errs)}")
    draft = result.get("draftOrder") or {}
    order = draft.get("order") or {}
    if order.get("id"):
        quote.order_gid = order["id"]
    return draft


def _fetch_delivery_profiles(shop_domain: str, token: str) -> list[dict]:
    """Tarifas fijas configuradas en deliveryProfiles (no requiere draft order)."""
    try:
        data = graphql_call(shop_domain, token, _DELIVERY_PROFILES, {})
    except Exception as e:
        logger.warning("No se pudieron obtener deliveryProfiles", error=str(e))
        return []
    rates: list[dict] = []
    seen: set[str] = set()
    for edge in ((data.get("deliveryProfiles") or {}).get("edges")) or []:
        node_profile = (edge or {}).get("node") or {}
        for lg in node_profile.get("profileLocationGroups") or []:
            for zone_edge in ((lg.get("locationGroupZones") or {}).get("edges")) or []:
                zone_node = (zone_edge or {}).get("node") or {}
                for method_edge in ((zone_node.get("methodDefinitions") or {}).get("edges")) or []:
                    m = (method_edge or {}).get("node") or {}
                    if not m.get("active"):
                        continue
                    name = m.get("name") or ""
                    provider = m.get("rateProvider") or {}
                    price_obj = provider.get("price") or {}
                    if not price_obj or name in seen:
                        continue
                    seen.add(name)
                    rates.append({
                        "title": name,
                        "handle": "",
                        "price": str(price_obj.get("amount") or "0"),
                        "currency": price_obj.get("currencyCode") or "CLP",
                    })
    logger.info("deliveryProfiles tarifas encontradas", count=len(rates))
    return rates


def fetch_shipping_rates(
    session: Session, quote: QuoteRequest, auth_header: str = ""
) -> list[dict]:
    """Devuelve las tarifas de envío disponibles para la cotización.

    1. Intenta obtener la tarifa desde el servicio shipping propio (usa los datos
       de la city de la CompanyLocation y el token del usuario).
    2. Si falla, cae a ``deliveryProfiles`` de Shopify (tarifas fijas).
    Nunca lanza excepción: devuelve [] ante cualquier error.
    """
    shop_domain, token = resolve_store_token(session, quote.shop_domain)

    # Obtener ciudad de la company location para calcular tarifa
    location_gid = (quote.company_location_gid or "").strip()
    if location_gid and auth_header:
        addr = _fetch_location_mailing_address(shop_domain, token, location_gid)
        city = (addr or {}).get("city") or ""
        if city:
            # Construir packages con pesos reales de cada variant
            variant_gids = [
                ln.variant_gid for ln in quote.lines if ln.variant_gid
            ]
            weight_map = _fetch_variant_weights(shop_domain, token, variant_gids)
            packages: list[dict] = []
            for ln in quote.lines:
                kg = weight_map.get(ln.variant_gid or "")
                if kg:
                    packages.append({"weight_kg": round(kg * int(ln.quantity or 1), 4)})
            logger.info(
                "Paquetes para cotizar envío",
                city=city,
                packages=packages,
                lines_count=len(quote.lines),
            )
            rates = fetch_rates_from_shipping_service(city, auth_header, packages or None)
            if rates:
                return rates

    return _fetch_delivery_profiles(shop_domain, token)
