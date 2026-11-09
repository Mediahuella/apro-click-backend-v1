"""Tests de la fecha de retiro en el webhook de pedidos.

La extensión de checkout ``fecha-retiro`` guarda ``fecha_retiro`` y
``sucursal_retiro`` como ``note_attributes``; el webhook los persiste y etiqueta
el pedido con ``retiro`` + ``retiro-AAAA-MM-DD``. Se mockea solo ``graphql_call``.

Ejecutar con:
  cd backend-v1 && python3 -m pytest src/services/orders/tests/test_pickup_date.py -v
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))
SHARED_ROOT = SERVICE_ROOT / "shared"
if str(SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(SHARED_ROOT))

from services import order_service  # noqa: E402

SHOP = "aproclick.myshopify.com"


def _order(**extra) -> dict:
    base = {
        "id": 6890258530488,
        "admin_graphql_api_id": "gid://shopify/Order/6890258530488",
        "note_attributes": [
            {"name": "Rut", "value": "76486379-8"},
            {"name": "fecha_retiro", "value": "2026-10-13"},
            {"name": "sucursal_retiro", "value": "Casa Matriz"},
        ],
        "tags": "",
    }
    base.update(extra)
    return base


class _GraphQLFake:
    def __init__(self, user_errors: list | None = None) -> None:
        self.llamadas: list[dict] = []
        self.user_errors = user_errors or []

    def __call__(self, shop_domain, token, query, variables=None, *, timeout=25):
        self.llamadas.append({"shop": shop_domain, "query": query, "variables": variables})
        return {"tagsAdd": {"userErrors": self.user_errors}}


@pytest.fixture
def graphql(monkeypatch) -> _GraphQLFake:
    fake = _GraphQLFake()
    monkeypatch.setattr(order_service, "graphql_call", fake)
    return fake


def test_lee_fecha_y_sucursal_de_note_attributes():
    assert order_service._pickup_from_order(_order()) == (date(2026, 10, 13), "Casa Matriz")


def test_sin_atributo_es_despacho():
    assert order_service._pickup_from_order(_order(note_attributes=[])) == (None, None)
    assert order_service._pickup_from_order({"id": 1}) == (None, None)


def test_fecha_mal_formada_se_descarta():
    order = _order(note_attributes=[{"name": "fecha_retiro", "value": "13-10-2026"}])
    assert order_service._pickup_from_order(order) == (None, None)


def test_etiqueta_el_pedido_con_retiro_y_fecha(graphql):
    order_service._tag_pickup_order(SHOP, "token", _order(), date(2026, 10, 13))
    assert len(graphql.llamadas) == 1
    llamada = graphql.llamadas[0]
    assert "tagsAdd" in llamada["query"]
    assert llamada["variables"] == {
        "id": "gid://shopify/Order/6890258530488",
        "tags": ["retiro", "retiro-2026-10-13"],
    }


def test_no_vuelve_a_etiquetar_si_ya_tiene_las_etiquetas(graphql):
    order = _order(tags="credito, Retiro, retiro-2026-10-13")
    order_service._tag_pickup_order(SHOP, "token", order, date(2026, 10, 13))
    assert graphql.llamadas == []


def test_agrega_solo_la_etiqueta_que_falta(graphql):
    order = _order(tags="retiro, retiro-2026-10-12")
    order_service._tag_pickup_order(SHOP, "token", order, date(2026, 10, 13))
    assert graphql.llamadas[0]["variables"]["tags"] == ["retiro-2026-10-13"]


def test_error_de_shopify_no_rompe_el_webhook(monkeypatch):
    def falla(*_args, **_kwargs):
        raise ValueError("Shopify HTTP 500")

    monkeypatch.setattr(order_service, "graphql_call", falla)
    order_service._tag_pickup_order(SHOP, "token", _order(), date(2026, 10, 13))
