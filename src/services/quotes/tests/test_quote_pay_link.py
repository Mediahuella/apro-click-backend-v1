"""Tests del link «Ir a pagar» (``GET /api/v1/quotes/{id}/pay?token=``).

El link redirige al ``invoiceUrl`` del Draft Order —el mismo que Shopify manda
por correo— y solo existe mientras la cotización tiene draft y aún no es pedido.

Ejecutar con:
  cd backend-v1 && python3 -m pytest src/services/quotes/tests/test_quote_pay_link.py -v
"""
from __future__ import annotations

import sys
import uuid as uuid_mod
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))
SHARED_ROOT = SERVICE_ROOT / "shared"
if str(SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(SHARED_ROOT))

from database.base import Base  # noqa: E402
import database.models  # noqa: E402,F401  registra todos los modelos en Base
from database.models.company import Company  # noqa: E402
from database.models.quote_request import QuoteRequest  # noqa: E402

from handlers import quotes as quotes_handler  # noqa: E402
from services import draft_order_service, quote_service  # noqa: E402
from utils.quotes_api_key import sign_pay_token, sign_pdf_token  # noqa: E402

INVOICE_URL = "https://aproclick.myshopify.com/12345/invoices/abc"


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("QUOTES_PDF_LINK_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("QUOTES_PUBLIC_BASE_URL", "https://api.example.com")


@pytest.fixture()
def crear_quote(monkeypatch):
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

    def _crear(**kwargs) -> str:
        with _get_session() as session:
            company = Company(id=uuid_mod.uuid4(), name="Empresa")
            session.add(company)
            session.flush()
            datos = {
                "status": "COTIZADA",
                "draft_order_gid": "gid://shopify/DraftOrder/1",
                **kwargs,
            }
            quote = QuoteRequest(
                id=uuid_mod.uuid4(),
                company_id=company.id,
                shopify_company_id="1",
                shop_domain="aproclick.myshopify.com",
                **datos,
            )
            session.add(quote)
            session.commit()
            return str(quote.id)

    return _crear


class _Ctx:
    function_name = "quotes"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-2:000000000000:function:quotes"
    aws_request_id = "00000000-0000-0000-0000-000000000000"


def _get(path: str, query: str) -> dict:
    event = {
        "version": "2.0",
        "routeKey": f"GET {path}",
        "rawPath": path,
        "rawQueryString": query,
        "queryStringParameters": dict(p.split("=", 1) for p in query.split("&") if p),
        "headers": {},
        "requestContext": {
            "http": {"method": "GET", "path": path, "protocol": "HTTP/1.1"},
            "domainName": "example.com",
            "stage": "$default",
            "requestId": "test",
        },
        "isBase64Encoded": False,
    }
    return quotes_handler.lambda_handler(event, _Ctx())


def _draft(monkeypatch, draft: dict) -> None:
    monkeypatch.setattr(draft_order_service, "fetch_invoice_url", lambda s, q: draft)


def test_redirige_al_invoice_url(crear_quote, monkeypatch):
    qid = crear_quote()
    _draft(monkeypatch, {"status": "OPEN", "invoiceUrl": INVOICE_URL, "order": None})
    resp = _get(f"/api/v1/quotes/{qid}/pay", f"token={sign_pay_token(qid)}")
    assert resp["statusCode"] == 302
    assert resp["headers"]["Location"] == INVOICE_URL


def test_token_del_pdf_no_sirve_para_pagar(crear_quote, monkeypatch):
    qid = crear_quote()
    _draft(monkeypatch, {"status": "OPEN", "invoiceUrl": INVOICE_URL, "order": None})
    resp = _get(f"/api/v1/quotes/{qid}/pay", f"token={sign_pdf_token(qid)}")
    assert resp["statusCode"] == 401


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "SOLICITADA", "draft_order_gid": None},
        {"status": "ACEPTADA", "order_gid": "gid://shopify/Order/9"},
        {"status": "ANULADA"},
    ],
)
def test_cotizacion_no_pagable_responde_409(crear_quote, monkeypatch, kwargs):
    qid = crear_quote(**kwargs)
    _draft(monkeypatch, {"status": "OPEN", "invoiceUrl": INVOICE_URL, "order": None})
    resp = _get(f"/api/v1/quotes/{qid}/pay", f"token={sign_pay_token(qid)}")
    assert resp["statusCode"] == 409


STATUS_URL = "https://aproclick.cl/94053990584/orders/xyz"
PEDIDO = {
    "id": "gid://shopify/Order/9",
    "name": "#1050",
    "displayFinancialStatus": "PAID",
    "statusPageUrl": STATUS_URL,
}


def test_cotizacion_ya_pagada_lleva_al_pedido_y_lo_enlaza(crear_quote, monkeypatch):
    """El link de un correo viejo, con el draft ya pagado: en vez de error, el
    cliente ve su pedido, y la cotización queda enlazada y ``ACEPTADA``."""
    qid = crear_quote(status="INVOICE_SENT")
    _draft(monkeypatch, {"status": "COMPLETED", "invoiceUrl": INVOICE_URL, "order": PEDIDO})
    resp = _get(f"/api/v1/quotes/{qid}/pay", f"token={sign_pay_token(qid)}")
    assert resp["statusCode"] == 302
    assert resp["headers"]["Location"] == STATUS_URL
    datos = quote_service.get_quote(qid, sync_order=False)
    assert datos["status"] == "ACEPTADA"
    assert datos["order_gid"] == "gid://shopify/Order/9"


def _estados(monkeypatch, estados: dict, llamadas: list | None = None) -> None:
    def _fake(session, shop_domain, gids):
        if llamadas is not None:
            llamadas.append(list(gids))
        return {g: estados[g] for g in gids if g in estados}

    monkeypatch.setattr(draft_order_service, "fetch_drafts_state", _fake)


def test_listado_detecta_cotizacion_pagada_en_shopify(crear_quote, monkeypatch):
    """El webhook del pedido no trae el Draft Order de origen: el listado se
    entera consultando los drafts abiertos y deja de ofrecer «Ir a pagar»."""
    pagada = crear_quote(draft_order_gid="gid://shopify/DraftOrder/1")
    abierta = crear_quote(draft_order_gid="gid://shopify/DraftOrder/2")
    llamadas: list = []
    _estados(
        monkeypatch,
        {
            "gid://shopify/DraftOrder/1": {"status": "COMPLETED", "order": PEDIDO},
            "gid://shopify/DraftOrder/2": {"status": "OPEN", "order": None},
        },
        llamadas,
    )
    datos = {q["id"]: q for q in quote_service.list_quotes(shopify_company_id="1")["items"]}
    assert len(llamadas) == 1  # una sola llamada en lote

    assert datos[pagada]["status"] == "ACEPTADA"
    assert "pay_url" not in datos[pagada]
    assert datos[pagada]["order"] == {
        "id": "9",
        "name": "#1050",
        "financial_status": "paid",
        "payment_state": "PAGADA",
    }
    assert "pay_url" in datos[abierta]
    assert "order" not in datos[abierta]

    # Persistido: la segunda lectura ya no consulta ese draft.
    llamadas.clear()
    quote_service.list_quotes(shopify_company_id="1")
    assert llamadas == [["gid://shopify/DraftOrder/2"]]


def test_listado_sigue_si_shopify_falla(crear_quote, monkeypatch):
    qid = crear_quote()

    def _falla(*a, **kw):
        raise RuntimeError("Shopify caído")

    monkeypatch.setattr(draft_order_service, "fetch_drafts_state", _falla)
    datos = quote_service.list_quotes(shopify_company_id="1")["items"]
    assert datos[0]["id"] == qid and "pay_url" in datos[0]


def test_pay_url_solo_en_cotizaciones_pagables(crear_quote, monkeypatch):
    _estados(monkeypatch, {})
    pagable = crear_quote()
    pendiente = crear_quote(status="SOLICITADA", draft_order_gid=None)
    con_pedido = crear_quote(status="ACEPTADA", order_gid="gid://shopify/Order/9")
    datos = {q["id"]: q for q in quote_service.list_quotes(shopify_company_id="1")["items"]}
    assert datos[pagable]["pay_url"] == (
        f"https://api.example.com/api/v1/quotes/{pagable}/pay?token={sign_pay_token(pagable)}"
    )
    assert "pay_url" not in datos[pendiente]
    assert "pay_url" not in datos[con_pedido]
