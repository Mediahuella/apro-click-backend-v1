"""Edición de pedidos en Shopify (Admin GraphQL: Order Edit API)."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from utils.shopify_graphql import (
    format_user_errors as _format_user_errors,
    graphql_call as _graphql,
    shopify_api_version as _api_version,
)

_ORDER_EDIT_BEGIN = """
mutation orderEditBegin($id: ID!) {
  orderEditBegin(id: $id) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

_CALC_ORDER_LINES_SHIPPING = """
query calcOrder($id: ID!) {
  node(id: $id) {
    ... on CalculatedOrder {
      id
      lineItems(first: 250) {
        nodes {
          id
          sku
          quantity
          variant {
            id
            sku
          }
        }
      }
      addedLineItems(first: 250) {
        nodes {
          id
          sku
          quantity
          variant {
            id
            sku
          }
        }
      }
      shippingLines {
        id
        title
      }
    }
  }
}
"""

_ORDER_EDIT_SET_QTY = """
mutation orderEditSetQuantity(
  $id: ID!
  $lineItemId: ID!
  $quantity: Int!
  $restock: Boolean
) {
  orderEditSetQuantity(
    id: $id
    lineItemId: $lineItemId
    quantity: $quantity
    restock: $restock
  ) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

_ORDER_EDIT_REMOVE_SHIPPING = """
mutation orderEditRemoveShippingLine($id: ID!, $shippingLineId: ID!) {
  orderEditRemoveShippingLine(id: $id, shippingLineId: $shippingLineId) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

_ORDER_EDIT_ADD_SHIPPING = """
mutation orderEditAddShippingLine(
  $id: ID!
  $shippingLine: OrderEditAddShippingLineInput!
) {
  orderEditAddShippingLine(id: $id, shippingLine: $shippingLine) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

_ORDER_EDIT_COMMIT = """
mutation orderEditCommit($id: ID!, $notify: Boolean!, $staffNote: String) {
  orderEditCommit(id: $id, notifyCustomer: $notify, staffNote: $staffNote) {
    order { id }
    userErrors { field message }
  }
}
"""

_VARIANT_BY_SKU = """
query VariantBySku($q: String!) {
  productVariants(first: 5, query: $q) {
    edges {
      node {
        id
        sku
      }
    }
  }
}
"""

_ORDER_EDIT_ADD_VARIANT = """
mutation orderEditAddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
  orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

_ORDER_INVOICE_SEND = """
mutation orderInvoiceSend($id: ID!, $email: EmailInput) {
  orderInvoiceSend(id: $id, email: $email) {
    order { id }
    userErrors { field message }
  }
}
"""


def send_order_invoice_via_shopify(
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
    *,
    email: dict[str, Any] | None = None,
) -> None:
    """Dispara ``orderInvoiceSend`` en Shopify para el pedido dado.

    Si ``email`` es ``None``, Shopify usa la dirección del cliente registrada
    en el pedido y la plantilla por defecto. Lanza ``ValueError`` con detalle
    si Shopify retorna ``userErrors``.
    """
    oid = str(shopify_order_id).strip()
    if not oid:
        raise ValueError("shopify_order_id vacío")
    order_gid = f"gid://shopify/Order/{oid}"
    variables: dict[str, Any] = {"id": order_gid}
    if email is not None:
        variables["email"] = email
    data = _graphql(shop_domain, access_token, _ORDER_INVOICE_SEND, variables)
    payload = data.get("orderInvoiceSend") or {}
    uerr = payload.get("userErrors") or []
    if uerr:
        raise ValueError(_format_user_errors(uerr))


def _parse_money(amount: Any) -> Decimal:
    try:
        return Decimal(str(amount).strip())
    except (InvalidOperation, TypeError, ValueError) as e:
        raise ValueError("Precio de envío inválido") from e


def _calc_order_node(
    shop_domain: str, access_token: str, calc_id: str
) -> dict[str, Any]:
    qdata = _graphql(
        shop_domain, access_token, _CALC_ORDER_LINES_SHIPPING, {"id": calc_id}
    )
    return qdata.get("node") or {}


def _calc_line_item_dicts(li_raw: Any) -> list[dict[str, Any]]:
    """Soporta conexión con `nodes` o `edges` (según versión Admin API)."""
    if not isinstance(li_raw, dict):
        return []
    nodes = li_raw.get("nodes")
    if isinstance(nodes, list) and nodes:
        return [n for n in nodes if isinstance(n, dict)]
    out: list[dict[str, Any]] = []
    for e in li_raw.get("edges") or []:
        n = (e or {}).get("node") if isinstance(e, dict) else None
        if isinstance(n, dict):
            out.append(n)
    return out


def _effective_line_sku(li: dict[str, Any]) -> str:
    """SKU en la línea o en la variante (Shopify a menudo deja `sku` vacío en la línea)."""
    raw = li.get("sku")
    if raw is not None and str(raw).strip():
        return str(raw).strip()
    var = li.get("variant")
    if isinstance(var, dict):
        raw = var.get("sku")
        if raw is not None and str(raw).strip():
            return str(raw).strip()
    return ""


def _sku_maps_from_node(
    node: dict[str, Any],
) -> tuple[dict[str, str], dict[str, int]]:
    # lineItems = líneas que ya existían (edición); addedLineItems = altas en ESTA sesión
    # (orderEditAddVariant no aparece en lineItems hasta el commit).
    calc_lines = _calc_line_item_dicts(node.get("lineItems")) + _calc_line_item_dicts(
        node.get("addedLineItems")
    )
    sku_to_line: dict[str, str] = {}
    sku_qty: dict[str, int] = {}
    for n in calc_lines:
        sku = _effective_line_sku(n)
        lid = n.get("id")
        if not sku or not lid:
            continue
        if sku in sku_to_line:
            raise ValueError(
                f"Hay más de una línea con SKU {sku!r} en el pedido; "
                "resolvé duplicados en Shopify."
            )
        sku_to_line[sku] = str(lid)
        sku_qty[sku] = int(n.get("quantity") or 0)
    return sku_to_line, sku_qty


def _shipping_line_dicts(sh_raw: Any) -> list[dict[str, Any]]:
    """`shippingLines` es lista directa en API reciente; no usa connection."""
    if isinstance(sh_raw, list):
        return [x for x in sh_raw if isinstance(x, dict) and x.get("id")]
    if isinstance(sh_raw, dict) and sh_raw.get("edges"):
        out: list[dict[str, Any]] = []
        for e in sh_raw.get("edges") or []:
            n = (e or {}).get("node") if isinstance(e, dict) else None
            if isinstance(n, dict) and n.get("id"):
                out.append(n)
        return out
    return []


def _variant_gid_from_sku(
    shop_domain: str, access_token: str, sku: str
) -> str:
    data = _graphql(
        shop_domain, access_token, _VARIANT_BY_SKU, {"q": f"sku:{sku}"}
    )
    pv = data.get("productVariants") or {}
    edges = pv.get("edges") or []
    if not edges:
        raise ValueError(
            f"No se encontró variante en catálogo con SKU {sku!r}"
        )
    first = edges[0]
    n = first.get("node") if isinstance(first, dict) else None
    if not isinstance(n, dict) or not n.get("id"):
        raise ValueError(f"Respuesta inválida al buscar SKU {sku!r}")
    return str(n["id"])


def apply_order_edits_via_shopify(
    shop_domain: str,
    access_token: str,
    shopify_order_id: str,
    *,
    line_items: list[dict[str, Any]] | None,
    shipping: dict[str, Any] | None,
    order_currency: str,
    restock_on_decrease: bool = True,
    staff_note: str | None = None,
) -> None:
    """Aplica cambios vía Order Edit y hace commit. Lanza ValueError si falla."""
    oid = str(shopify_order_id).strip()
    if not oid:
        raise ValueError("shopify_order_id vacío")
    order_gid = f"gid://shopify/Order/{oid}"

    data = _graphql(
        shop_domain,
        access_token,
        _ORDER_EDIT_BEGIN,
        {"id": order_gid},
    )
    begin = data.get("orderEditBegin") or {}
    uerr = begin.get("userErrors") or []
    if uerr:
        raise ValueError(_format_user_errors(uerr))

    calc = begin.get("calculatedOrder") or {}
    calc_id = calc.get("id")
    if not calc_id:
        raise ValueError("Shopify no devolvió calculatedOrder para la edición")

    try:
        if line_items:
            seen_req: set[str] = set()
            parsed: list[tuple[str, int]] = []
            for raw in line_items:
                if not isinstance(raw, dict):
                    raise ValueError(
                        "Cada elemento de shopify_line_items debe ser objeto"
                    )
                sku = str(raw.get("sku") or "").strip()
                if not sku:
                    raise ValueError("Cada línea debe incluir sku")
                if sku in seen_req:
                    raise ValueError(f"SKU duplicado en la petición: {sku}")
                seen_req.add(sku)
                q = raw.get("quantity")
                try:
                    qty = int(q)
                except (TypeError, ValueError) as e:
                    raise ValueError("quantity debe ser entero >= 0") from e
                if qty < 0:
                    raise ValueError("quantity debe ser entero >= 0")
                parsed.append((sku, qty))

            node = _calc_order_node(shop_domain, access_token, calc_id)
            sku_to_line, sku_qty = _sku_maps_from_node(node)
            initial_skus = set(sku_to_line.keys())

            added_any = False
            for sku, qty in parsed:
                if sku not in initial_skus:
                    if qty <= 0:
                        raise ValueError(
                            f"Para agregar la variante {sku!r} la cantidad debe ser "
                            "mayor a 0"
                        )
                    variant_gid = _variant_gid_from_sku(
                        shop_domain, access_token, sku
                    )
                    d_add = _graphql(
                        shop_domain,
                        access_token,
                        _ORDER_EDIT_ADD_VARIANT,
                        {
                            "id": calc_id,
                            "variantId": variant_gid,
                            "quantity": qty,
                        },
                    )
                    add_payload = d_add.get("orderEditAddVariant") or {}
                    u_add = add_payload.get("userErrors") or []
                    if u_add:
                        raise ValueError(_format_user_errors(u_add))
                    added_any = True

            if added_any:
                node = _calc_order_node(shop_domain, access_token, calc_id)
                sku_to_line, sku_qty = _sku_maps_from_node(node)

            for sku, qty in parsed:
                lid = sku_to_line.get(sku)
                if not lid:
                    raise ValueError(
                        f"No hay línea con SKU {sku!r} tras agregar variantes"
                    )
                prev = sku_qty.get(sku, 0)
                restock = bool(restock_on_decrease and qty < prev)

                d2 = _graphql(
                    shop_domain,
                    access_token,
                    _ORDER_EDIT_SET_QTY,
                    {
                        "id": calc_id,
                        "lineItemId": lid,
                        "quantity": qty,
                        "restock": restock,
                    },
                )
                payload = d2.get("orderEditSetQuantity") or {}
                u2 = payload.get("userErrors") or []
                if u2:
                    raise ValueError(_format_user_errors(u2))

        if shipping:
            node = _calc_order_node(shop_domain, access_token, calc_id)
            title = str(shipping.get("title") or "").strip()
            if not title:
                raise ValueError("shopify_shipping.title es obligatorio")
            price_amt = _parse_money(shipping.get("price"))
            if price_amt < 0:
                raise ValueError("shopify_shipping.price no puede ser negativo")

            cur = (order_currency or "USD").strip().upper()
            if len(cur) != 3:
                cur = "USD"

            ship_nodes = _shipping_line_dicts(node.get("shippingLines"))
            for sn in ship_nodes:
                sid = str(sn.get("id") or "")
                if not sid:
                    continue
                d_rm = _graphql(
                    shop_domain,
                    access_token,
                    _ORDER_EDIT_REMOVE_SHIPPING,
                    {"id": calc_id, "shippingLineId": sid},
                )
                p_rm = d_rm.get("orderEditRemoveShippingLine") or {}
                u_rm = p_rm.get("userErrors") or []
                if u_rm:
                    raise ValueError(_format_user_errors(u_rm))

            d_add = _graphql(
                shop_domain,
                access_token,
                _ORDER_EDIT_ADD_SHIPPING,
                {
                    "id": calc_id,
                    "shippingLine": {
                        "title": title,
                        "price": {
                            "amount": str(price_amt),
                            "currencyCode": cur,
                        },
                    },
                },
            )
            p_add = d_add.get("orderEditAddShippingLine") or {}
            u_add = p_add.get("userErrors") or []
            if u_add:
                raise ValueError(_format_user_errors(u_add))

        d4 = _graphql(
            shop_domain,
            access_token,
            _ORDER_EDIT_COMMIT,
            {
                "id": calc_id,
                "notify": False,
                "staffNote": staff_note,
            },
        )
        payload4 = d4.get("orderEditCommit") or {}
        u4 = payload4.get("userErrors") or []
        if u4:
            raise ValueError(_format_user_errors(u4))
    except Exception:
        # Sin mutación de abandon portable entre versiones; la sesión caduca en Shopify.
        raise


def fetch_order_rest(
    shop_domain: str, access_token: str, shopify_order_id: str
) -> dict[str, Any]:
    """GET /orders/{id}.json — mismo shape que usa el webhook para upsert."""
    shop = shop_domain.strip().lower()
    ver = _api_version()
    oid = str(shopify_order_id).strip()
    url = f"https://{shop}/admin/api/{ver}/orders/{oid}.json"
    req = Request(url, headers={"X-Shopify-Access-Token": access_token})
    try:
        with urlopen(req, timeout=25) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except OSError:
            detail = str(e.code)
        raise ValueError(f"Shopify REST {e.code}: {detail[:500]}") from e
    except (OSError, json.JSONDecodeError, UnicodeError) as e:
        raise ValueError(f"Error leyendo pedido en Shopify: {e}") from e
    if not isinstance(body, dict):
        raise ValueError("Respuesta REST inválida")
    order = body.get("order")
    if not isinstance(order, dict):
        raise ValueError("Pedido no encontrado en Shopify")
    return order
