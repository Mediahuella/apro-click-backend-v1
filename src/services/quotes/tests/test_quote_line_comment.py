"""Comentario por línea de cotización (``QuoteRequestLine.comment``).

Guardar solo comentarios no debe tocar el Draft Order, el PDF ni APRO IA:
el comentario es interno del panel.

Ejecutar con:
  cd backend-v1 && python3 -m pytest src/services/quotes/tests/test_quote_line_comment.py -v
"""
from __future__ import annotations

import uuid as uuid_mod
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from database.base import Base
import database.models  # noqa: F401  registra todos los modelos en Base
from database.models.quote_request import QuoteRequest, QuoteRequestLine
from services import draft_order_service, quote_service


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture()
def cotizada(monkeypatch):
    """Cotización ya negociada (con draft y folio) y espías de los efectos."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    @contextmanager
    def _get_session():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(quote_service, "get_session", _get_session)
    efectos: list[str] = []
    monkeypatch.setattr(
        draft_order_service,
        "update_quote_draft",
        lambda *a, **kw: efectos.append("draft"),
    )
    monkeypatch.setattr(
        quote_service, "_sync_apro_ia_and_pdf", lambda *a, **kw: efectos.append("pdf")
    )

    with _get_session() as session:
        quote = QuoteRequest(
            id=uuid_mod.uuid4(),
            shop_domain="aproclick.myshopify.com",
            folio="CO-900100",
            status="COTIZADA",
            draft_order_gid="gid://shopify/DraftOrder/1",
        )
        quote.lines.append(
            QuoteRequestLine(
                line_number=1,
                variant_gid="gid://shopify/ProductVariant/1",
                sku="SKU-1",
                quantity=2,
            )
        )
        session.add(quote)
        session.commit()
        return str(quote.id), str(quote.lines[0].id), efectos


def test_guarda_comentario_sin_tocar_shopify_ni_apro_ia(cotizada):
    qid, line_id, efectos = cotizada
    data = quote_service.update_quote_lines(
        qid, {"lines": [{"id": line_id, "comment": "  Entrega en 15 días  "}]}
    )
    assert data["lines"][0]["comment"] == "Entrega en 15 días"
    assert efectos == []


def test_comentario_vacio_lo_borra(cotizada):
    qid, line_id, _ = cotizada
    quote_service.update_quote_lines(qid, {"lines": [{"id": line_id, "comment": "x"}]})
    data = quote_service.update_quote_lines(
        qid, {"lines": [{"id": line_id, "comment": "   "}]}
    )
    assert data["lines"][0]["comment"] is None


def test_cambio_de_precio_sigue_actualizando_el_draft(cotizada):
    qid, line_id, efectos = cotizada
    quote_service.update_quote_lines(
        qid,
        {"lines": [{"id": line_id, "negotiated_unit_price": "1000", "comment": "ok"}]},
    )
    assert efectos == ["draft", "pdf"]


# ---------------------------------------------------------------------------
# Productos que se mandan a APRO IA: envío como línea y comentario por línea
# ---------------------------------------------------------------------------
from services import apro_ia_client  # noqa: E402


def _linea(**kw):
    base = dict(line_number=1, sku="SKU-1", title="Casco", quantity=2,
                negotiated_unit_price=1000, comment=None)
    base.update(kw)
    return QuoteRequestLine(**base)


def test_envio_va_como_linea_costo_de_envio():
    productos = apro_ia_client.build_productos(
        [_linea()], {"title": "Starken", "price": "3990.00"}, include_comments=False
    )
    assert productos[-1] == {
        "sku": apro_ia_client.DEFAULT_SHIPPING_SKU,
        "name": "COSTO DE ENVIO - Starken",
        "price": 3990,
        "amount": 1,
        "total": 3990,
    }
    assert len(productos) == 2


def test_sin_envio_no_agrega_linea():
    assert len(apro_ia_client.build_productos([_linea()], None)) == 1


def test_comentario_de_linea_solo_con_el_flag(monkeypatch):
    lineas = [_linea(comment="Entrega en 15 días")]
    monkeypatch.delenv("APRO_IA_LINE_COMMENTS", raising=False)
    assert "comment_product" not in apro_ia_client.build_productos(lineas)[0]
    monkeypatch.setenv("APRO_IA_LINE_COMMENTS", "true")
    producto = apro_ia_client.build_productos(lineas)[0]
    assert producto["comment_product"] == "Entrega en 15 días"
    assert "comment" not in producto


def test_comentario_viaja_a_apro_ia_con_el_flag(cotizada, monkeypatch):
    qid, line_id, efectos = cotizada
    with quote_service.get_session() as session:
        quote = session.get(QuoteRequest, uuid_mod.UUID(qid))
        quote.apro_ia_folio = "AC-000999"
        session.commit()
    monkeypatch.setenv("APRO_IA_LINE_COMMENTS", "true")

    quote_service.update_quote_lines(qid, {"lines": [{"id": line_id, "comment": "ok"}]})

    assert efectos == ["pdf"]  # APRO IA + PDF, sin reescribir el Draft Order
