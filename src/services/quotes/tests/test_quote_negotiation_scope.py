"""Tests de alcance (scope) de los endpoints de negociación de cotizaciones.

``_require_negotiator`` solo valida el ROL (SALES/ADMIN/SUPERADMIN) — sin la
verificación agregada en ``_ensure_quote_in_scope``, cualquier SALES podía
negociar/enviar invoice/completar/cotizar envío de una cotización de una
empresa que no le corresponde, con solo conocer el ``quote_id`` (UUID).

Estos tests reproducen ese escenario contra una BD SQLite en memoria: un
SALES vinculado solo a la Empresa A intenta operar sobre una cotización de
la Empresa B → debe recibir 404 (no debe poder distinguir "no autorizado" de
"no existe", igual que el patrón ya usado en el listado / detalle).

Ejecutar con:
  cd backend-v1 && python3 -m pytest src/services/quotes/tests/test_quote_negotiation_scope.py -v
"""
from __future__ import annotations

import json
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
from database.models.quote_request import QuoteRequest, QuoteRequestLine  # noqa: E402
from database.models.user import User  # noqa: E402
from database.models.user_company import UserCompany  # noqa: E402

from handlers import quotes as quotes_handler  # noqa: E402
from services import quote_service  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture()
def db_session_factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    @contextmanager
    def _get_session():
        session = Session(engine)
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(quote_service, "get_session", _get_session)
    return _get_session


@pytest.fixture()
def scenario(db_session_factory):
    """Empresa A (con SALES vinculado) + Empresa B (sin ese SALES) + una
    QuoteRequest que pertenece a la Empresa B."""
    with db_session_factory() as session:
        company_a = Company(id=uuid_mod.uuid4(), name="Empresa A (del vendedor)")
        company_b = Company(id=uuid_mod.uuid4(), name="Empresa B (ajena)")
        sales_user = User(
            id=uuid_mod.uuid4(),
            cognito_sub="sub-sales-1",
            email="vendedor@apro.cl",
            role="SALES",
            status="ACTIVE",
        )
        session.add_all([company_a, company_b, sales_user])
        session.flush()
        session.add(UserCompany(user_id=sales_user.id, company_id=company_a.id))

        quote_b = QuoteRequest(
            id=uuid_mod.uuid4(),
            company_id=company_b.id,
            shopify_company_id="gid://shopify/Company/2",
            shop_domain="aproclick.myshopify.com",
            folio="CO-900001",
            status="COTIZADA",
            draft_order_gid="gid://shopify/DraftOrder/999",
        )
        quote_b.lines.append(
            QuoteRequestLine(
                line_number=1,
                variant_gid="gid://shopify/ProductVariant/1",
                sku="SKU-1",
                title="Producto de prueba",
                quantity=1,
            )
        )
        session.add(quote_b)
        session.flush()
        ids = {
            "company_a": str(company_a.id),
            "company_b": str(company_b.id),
            "sales_user_id": str(sales_user.id),
            "quote_b_id": str(quote_b.id),
        }
        session.commit()
        return ids


def _sales_user_dict(sales_user_id: str, company_a_id: str) -> dict:
    return {
        "id": sales_user_id,
        "email": "vendedor@apro.cl",
        "role": "SALES",
        "status": "ACTIVE",
        "order_company_ids": [company_a_id],
    }


class _FakeLambdaContext:
    function_name = "apro-click-admin-quotes-dev-quotes"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-2:000000000000:function:quotes"
    aws_request_id = "00000000-0000-0000-0000-000000000000"


def _event(method: str, path: str, body: dict | None = None) -> dict:
    return {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {
            "content-type": "application/json",
            "authorization": "Bearer fake-token-sales",
        },
        "requestContext": {
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1"},
            "domainName": "example.com",
            "stage": "$default",
            "requestId": "test-request-id",
        },
        "body": json.dumps(body) if body is not None else "",
        "isBase64Encoded": False,
    }


@pytest.fixture(autouse=True)
def _fake_cognito(monkeypatch, scenario):
    """Cualquier Bearer token resuelve al SALES vinculado solo a Empresa A —
    sin llamar a Cognito de verdad."""
    monkeypatch.setattr(
        quotes_handler,
        "get_user_by_cognito_access_token",
        lambda token: _sales_user_dict(scenario["sales_user_id"], scenario["company_a"]),
    )


ACTIONS = [
    ("POST", "draft"),
    ("POST", "invoice"),
    ("POST", "complete"),
    ("PATCH", None),
    ("GET", "shipping-rates"),
]


@pytest.mark.parametrize("method,suffix", ACTIONS)
def test_sales_fuera_de_alcance_recibe_404(scenario, monkeypatch, method, suffix):
    """El SALES solo tiene acceso a Empresa A; la cotización es de Empresa B
    → debe recibir 404 en todos los endpoints de negociación, sin que la
    lógica de negocio (Shopify/SQS) llegue a ejecutarse."""
    called = []
    monkeypatch.setattr(
        quote_service, "negotiate_draft", lambda *a, **kw: called.append(1) or {}
    )
    monkeypatch.setattr(
        quote_service, "update_quote_lines", lambda *a, **kw: called.append(1) or {}
    )
    monkeypatch.setattr(
        quote_service, "send_invoice", lambda *a, **kw: called.append(1) or {}
    )
    monkeypatch.setattr(quote_service, "complete", lambda *a, **kw: called.append(1) or {})
    monkeypatch.setattr(
        quote_service, "get_shipping_rates", lambda *a, **kw: called.append(1) or []
    )

    quote_id = scenario["quote_b_id"]
    path = f"/api/v1/quotes/{quote_id}" + (f"/{suffix}" if suffix else "")
    event = _event(method, path, body={} if method in ("POST", "PATCH") else None)

    response = quotes_handler.lambda_handler(event, _FakeLambdaContext())

    assert response["statusCode"] == 404
    assert called == []  # el guard de scope corta ANTES de tocar Shopify/SQS


@pytest.mark.parametrize("method,suffix", ACTIONS)
def test_sales_dentro_de_alcance_pasa_el_guard(scenario, monkeypatch, method, suffix):
    """Misma cotización pero reasignada a la Empresa A del vendedor: el guard
    de scope debe dejar pasar la llamada hasta la lógica de negocio."""
    with sqlite_session(scenario) as session:
        quote = session.get(QuoteRequest, uuid_mod.UUID(scenario["quote_b_id"]))
        quote.company_id = uuid_mod.UUID(scenario["company_a"])
        session.commit()

    called = []
    monkeypatch.setattr(
        quote_service, "negotiate_draft", lambda *a, **kw: called.append(1) or {}
    )
    monkeypatch.setattr(
        quote_service, "update_quote_lines", lambda *a, **kw: called.append(1) or {}
    )
    monkeypatch.setattr(
        quote_service, "send_invoice", lambda *a, **kw: called.append(1) or {}
    )
    monkeypatch.setattr(quote_service, "complete", lambda *a, **kw: called.append(1) or {})
    monkeypatch.setattr(
        quote_service, "get_shipping_rates", lambda *a, **kw: called.append(1) or []
    )

    quote_id = scenario["quote_b_id"]
    path = f"/api/v1/quotes/{quote_id}" + (f"/{suffix}" if suffix else "")
    event = _event(method, path, body={} if method in ("POST", "PATCH") else None)

    response = quotes_handler.lambda_handler(event, _FakeLambdaContext())

    assert response["statusCode"] == 200
    assert called == [1]


@contextmanager
def sqlite_session(scenario):
    """Reabre una sesión sobre el mismo engine sqlite que ``quote_service``
    está usando (monkeypatcheado por el fixture ``db_session_factory``)."""
    with quote_service.get_session() as session:
        yield session
