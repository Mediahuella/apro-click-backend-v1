"""Tests de ``draft_order_service.mark_order_paid`` (mutaciones a Shopify).

Complementan ``test_apro_ia_transferencias.py``, que mockea
``draft_order_service`` entero y por lo tanto no ejercita las llamadas
GraphQL. Acá se mockea solo ``graphql_call`` y se verifica **qué** se le manda
a Shopify cuando llega una transferencia ``Aprobada``:
``orderMarkAsPaid`` sobre el pedido + ``orderUpdate`` con el tag
``transferencia`` y la nota con el folio.

Ejecutar con:
  cd backend-v1 && python3 -m pytest src/services/quotes/tests/test_mark_order_paid.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))
SHARED_ROOT = SERVICE_ROOT / "shared"
if str(SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(SHARED_ROOT))

from services import draft_order_service  # noqa: E402

ORDER_GID = "gid://shopify/Order/6890258530488"
SHOP = "aproclick.myshopify.com"


class _ShopifyGraphQLFake:
    """Fake de ``graphql_call`` que registra la secuencia de operaciones."""

    def __init__(self, order: dict) -> None:
        self.order = order
        self.llamadas: list[tuple[str, dict]] = []

    def __call__(self, shop_domain, token, query, variables=None):
        variables = variables or {}
        if "query estadoPedido" in query:
            self.llamadas.append(("estadoPedido", variables))
            return {"order": self.order}
        if "mutation marcarPedidoPagado" in query:
            self.llamadas.append(("orderMarkAsPaid", variables))
            pagado = dict(self.order, canMarkAsPaid=False, displayFinancialStatus="PAID")
            return {"orderMarkAsPaid": {"order": pagado, "userErrors": []}}
        if "mutation etiquetarPedido" in query:
            self.llamadas.append(("orderUpdate", variables))
            return {"orderUpdate": {"order": self.order, "userErrors": []}}
        raise AssertionError(f"Operación inesperada: {query[:60]}")

    @property
    def operaciones(self) -> list[str]:
        return [nombre for nombre, _ in self.llamadas]

    def variables_de(self, nombre: str) -> dict:
        return next(v for n, v in self.llamadas if n == nombre)


def _pedido(**overrides) -> dict:
    base = {
        "id": ORDER_GID,
        "name": "#1055",
        "canMarkAsPaid": True,
        "displayFinancialStatus": "PENDING",
        "cancelledAt": None,
        "tags": [],
        "note": None,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def shopify(monkeypatch):
    """Instala el fake y evita la resolución real del token de la tienda."""

    def _instalar(order: dict | None) -> _ShopifyGraphQLFake:
        fake = _ShopifyGraphQLFake(order or {})
        monkeypatch.setattr(draft_order_service, "graphql_call", fake)
        monkeypatch.setattr(
            draft_order_service,
            "resolve_store_token",
            lambda session, shop_domain="": (shop_domain or SHOP, "shpat_fake"),
        )
        return fake

    return _instalar


def test_pedido_impago_se_marca_pagado_y_se_etiqueta(shopify):
    """El caso normal: transferencia Aprobada sobre un pedido pendiente."""
    fake = shopify(_pedido())

    resultado = draft_order_service.mark_order_paid(
        None, ORDER_GID, shop_domain=SHOP, transfer_folio="TR-000123"
    )

    assert fake.operaciones == ["estadoPedido", "orderMarkAsPaid", "orderUpdate"]
    assert fake.variables_de("orderMarkAsPaid") == {"input": {"id": ORDER_GID}}
    update = fake.variables_de("orderUpdate")["input"]
    assert update["id"] == ORDER_GID
    assert update["tags"] == ["transferencia"]
    assert update["note"] == "Pagado por transferencia (APRO IA folio TR-000123)"
    assert resultado["already_paid"] is False
    assert resultado["order"]["displayFinancialStatus"] == "PAID"


def test_pedido_ya_pagado_no_reintenta_la_mutacion(shopify):
    """Idempotencia: un reintento del webhook no vuelve a cobrar ni falla."""
    fake = shopify(_pedido(canMarkAsPaid=False, displayFinancialStatus="PAID"))

    resultado = draft_order_service.mark_order_paid(
        None, ORDER_GID, shop_domain=SHOP, transfer_folio="TR-000123"
    )

    assert "orderMarkAsPaid" not in fake.operaciones
    assert resultado["already_paid"] is True
    # El tag igual se aplica: puede ser el dato que falte.
    assert fake.variables_de("orderUpdate")["input"]["tags"] == ["transferencia"]


def test_tag_y_nota_ya_presentes_no_generan_orderUpdate(shopify):
    """Reproceso completo de una transferencia ya aplicada: cero escrituras."""
    fake = shopify(
        _pedido(
            canMarkAsPaid=False,
            tags=["transferencia"],
            note="Pagado por transferencia (APRO IA folio TR-000123)",
        )
    )

    draft_order_service.mark_order_paid(
        None, ORDER_GID, shop_domain=SHOP, transfer_folio="TR-000123"
    )

    assert fake.operaciones == ["estadoPedido"]


def test_conserva_tags_y_nota_previos(shopify):
    """El tag y la nota se agregan, no reemplazan lo que ya tenía el pedido."""
    fake = shopify(_pedido(tags=["b2b", "urgente"], note="Retira en bodega"))

    draft_order_service.mark_order_paid(
        None, ORDER_GID, shop_domain=SHOP, transfer_folio="TR-000123"
    )

    update = fake.variables_de("orderUpdate")["input"]
    assert update["tags"] == ["b2b", "urgente", "transferencia"]
    assert update["note"] == (
        "Retira en bodega\nPagado por transferencia (APRO IA folio TR-000123)"
    )


def test_pedido_anulado_no_se_marca_pagado(shopify):
    """Un pedido cancelado en Shopify no admite pago: error explícito."""
    fake = shopify(_pedido(cancelledAt="2026-08-01T12:00:00Z"))

    with pytest.raises(ValueError, match="anulado"):
        draft_order_service.mark_order_paid(None, ORDER_GID, shop_domain=SHOP)

    assert fake.operaciones == ["estadoPedido"]


def test_pedido_inexistente_en_shopify(shopify):
    shopify(None)

    with pytest.raises(ValueError, match="no existe"):
        draft_order_service.mark_order_paid(None, ORDER_GID, shop_domain=SHOP)


def test_sin_order_gid(shopify):
    shopify(_pedido())

    with pytest.raises(ValueError, match="Falta el pedido"):
        draft_order_service.mark_order_paid(None, "", shop_domain=SHOP)


def test_user_errors_de_shopify_se_propagan(monkeypatch):
    """Un userError en orderMarkAsPaid no puede pasar por éxito silencioso."""

    def _graphql(shop_domain, token, query, variables=None):
        if "query estadoPedido" in query:
            return {"order": _pedido()}
        return {
            "orderMarkAsPaid": {
                "order": None,
                "userErrors": [{"field": ["input"], "message": "Order cannot be marked as paid"}],
            }
        }

    monkeypatch.setattr(draft_order_service, "graphql_call", _graphql)
    monkeypatch.setattr(
        draft_order_service,
        "resolve_store_token",
        lambda session, shop_domain="": (SHOP, "shpat_fake"),
    )

    with pytest.raises(ValueError, match="orderMarkAsPaid"):
        draft_order_service.mark_order_paid(None, ORDER_GID, shop_domain=SHOP)


def test_fallo_al_etiquetar_no_invalida_el_pago(monkeypatch):
    """El tag es best-effort: el pedido ya quedó pagado y eso manda."""
    estado = _pedido()

    def _graphql(shop_domain, token, query, variables=None):
        if "query estadoPedido" in query:
            return {"order": estado}
        if "mutation marcarPedidoPagado" in query:
            return {
                "orderMarkAsPaid": {
                    "order": dict(estado, canMarkAsPaid=False),
                    "userErrors": [],
                }
            }
        raise RuntimeError("Shopify caído")

    monkeypatch.setattr(draft_order_service, "graphql_call", _graphql)
    monkeypatch.setattr(
        draft_order_service,
        "resolve_store_token",
        lambda session, shop_domain="": (SHOP, "shpat_fake"),
    )

    resultado = draft_order_service.mark_order_paid(
        None, ORDER_GID, shop_domain=SHOP, transfer_folio="TR-000123"
    )
    assert resultado["already_paid"] is False


# ---------------------------------------------------------------------------
# Pago rechazado: el pedido se etiqueta, nunca se anula
# ---------------------------------------------------------------------------
def test_rechazo_etiqueta_el_pedido_y_lo_deja_pendiente(shopify):
    """El caso normal: el pedido nació impago y así se queda."""
    fake = shopify(_pedido())

    resultado = draft_order_service.tag_order_payment_rejected(
        None,
        ORDER_GID,
        shop_domain=SHOP,
        transfer_folio="TR-000123",
        raw_status="Descartada",
    )

    assert fake.operaciones == ["estadoPedido", "orderUpdate"]
    update = fake.variables_de("orderUpdate")["input"]
    assert update["tags"] == ["transferencia-rechazada"]
    assert update["note"] == (
        "Pago con transferencia rechazado en APRO IA folio TR-000123 "
        "(Descartada). El pedido queda pendiente de pago."
    )
    assert resultado["already_tagged"] is False
    assert resultado["was_paid"] is False


def test_rechazo_de_credito_usa_su_propio_tag(shopify):
    fake = shopify(_pedido(tags=["b2b"]))

    draft_order_service.tag_order_payment_rejected(
        None,
        ORDER_GID,
        shop_domain=SHOP,
        payment_method=draft_order_service.CREDIT_TAG,
        transfer_folio="TR-000999",
        raw_status="Anulada",
    )

    update = fake.variables_de("orderUpdate")["input"]
    assert update["tags"] == ["b2b", "credito-rechazado"]
    assert "Pago con crédito rechazado" in update["note"]


def test_rechazo_sobre_pedido_ya_pagado_advierte_en_la_nota(shopify):
    """No se revierte el pago: orderMarkAsPaid no tiene inverso en Shopify."""
    fake = shopify(_pedido(canMarkAsPaid=False, displayFinancialStatus="PAID"))

    resultado = draft_order_service.tag_order_payment_rejected(
        None, ORDER_GID, shop_domain=SHOP, raw_status="Descartada"
    )

    assert "orderCancel" not in fake.operaciones
    assert resultado["was_paid"] is True
    assert "ATENCIÓN" in fake.variables_de("orderUpdate")["input"]["note"]


def test_rechazo_ya_etiquetado_no_genera_orderUpdate(shopify):
    """Idempotencia: un reenvío del mismo rechazo no reescribe el pedido."""
    nota = (
        "Pago con transferencia rechazado en APRO IA folio TR-000123 "
        "(Descartada). El pedido queda pendiente de pago."
    )
    fake = shopify(_pedido(tags=["transferencia-rechazada"], note=nota))

    resultado = draft_order_service.tag_order_payment_rejected(
        None,
        ORDER_GID,
        shop_domain=SHOP,
        transfer_folio="TR-000123",
        raw_status="Descartada",
    )

    assert fake.operaciones == ["estadoPedido"]
    assert resultado["already_tagged"] is True
