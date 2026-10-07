"""El detalle/listado de cotizaciones expone RUT de la empresa y contacto.

``_attach_contact`` resuelve el ``Client`` de quien pidió la cotización (por
email o customer de Shopify) y, si no está registrado, el contacto principal.

Ejecutar con:
  cd backend-v1 && python3 -m pytest src/services/quotes/tests/test_quote_contact.py -v
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
from database.models.client import Client
from database.models.company import Company
from database.models.quote_request import QuoteRequest
from services import quote_service


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture()
def session_factory(monkeypatch):
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
    return _get_session


def _crear(session_factory, *, email: str | None, customer_id: str | None = None):
    with session_factory() as session:
        company = Company(
            id=uuid_mod.uuid4(), name="Seba SPA", billing_rut="76486379-8"
        )
        session.add(company)
        session.flush()
        session.add_all(
            [
                Client(
                    company_id=company.id,
                    email="principal@seba.cl",
                    name="Principal",
                    phone="+56911111111",
                    is_main_contact=True,
                ),
                Client(
                    company_id=company.id,
                    email="Compras@Seba.cl",
                    name="Compras",
                    phone="+56922222222",
                    shopify_customer_id="gid://shopify/Customer/77",
                    is_main_contact=False,
                ),
            ]
        )
        quote = QuoteRequest(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            shop_domain="aproclick.myshopify.com",
            requested_by_email=email,
            requested_by_customer_id=customer_id,
        )
        session.add(quote)
        session.commit()
        return str(quote.id)


def test_contacto_del_solicitante_por_email(session_factory):
    qid = _crear(session_factory, email="compras@seba.cl")
    data = quote_service.get_quote(qid)
    assert data["company_rut"] == "76486379-8"
    assert data["contact"]["name"] == "Compras"
    assert data["contact"]["phone"] == "+56922222222"


def test_contacto_por_customer_de_shopify(session_factory):
    qid = _crear(session_factory, email="otro@seba.cl", customer_id="77")
    assert quote_service.get_quote(qid)["contact"]["name"] == "Compras"


def test_cae_al_contacto_principal(session_factory):
    qid = _crear(session_factory, email="desconocido@x.cl")
    data = quote_service.list_quotes()["items"][0]
    assert str(data["id"]) == qid
    assert data["contact"]["email"] == "principal@seba.cl"
    assert data["contact"]["is_main_contact"] is True


def test_sin_solicitante_ni_principal_usa_contacto_con_telefono(session_factory):
    """Cotización creada desde APRO IA (sin email) en una empresa sin
    contacto principal marcado."""
    with session_factory() as session:
        company = Company(id=uuid_mod.uuid4(), name="Sin principal SPA")
        session.add(company)
        session.flush()
        session.add_all(
            [
                Client(company_id=company.id, email="a@x.cl", name="Sin fono"),
                Client(
                    company_id=company.id,
                    email="b@x.cl",
                    name="Con fono",
                    phone="912345678",
                ),
            ]
        )
        quote = QuoteRequest(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            shop_domain="aproclick.myshopify.com",
        )
        session.add(quote)
        session.commit()
        qid = str(quote.id)
    assert quote_service.get_quote(qid)["contact"]["phone"] == "912345678"
