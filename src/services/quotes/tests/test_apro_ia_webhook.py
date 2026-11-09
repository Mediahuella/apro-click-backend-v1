"""Tests del webhook de eventos de APRO IA (ver docs/Webhook eventos.md).

Simulan payloads con la forma exacta que documenta APRO IA
(``quotation.created|updated|approved|rejected``) contra:

  1. ``quote_service.handle_apro_ia_webhook`` — lógica de negocio, con una BD
     SQLite en memoria (sin tocar la BD real).
  2. ``handlers.quotes.lambda_handler`` — el endpoint HTTP completo
     (``POST /api/v1/quotes/webhooks/apro-ia``), simulando el evento API
     Gateway HTTP API v2 tal como llegaría desde APRO IA.

No requieren credenciales ni red: no hacen ninguna llamada real a APRO IA,
Shopify ni SQS (se monkeypatchea ``encolar_cotizacion_apro_ia_estado``).

Ejecutar con:
  cd backend-v1 && python3 -m pytest src/services/quotes/tests/test_apro_ia_webhook.py -v
"""
from __future__ import annotations

import json
import sys
import uuid as uuid_mod
from contextlib import contextmanager
from decimal import Decimal
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

from handlers import quotes as quotes_handler  # noqa: E402
from services import quote_service  # noqa: E402
from utils.quotes_api_key import require_apro_ia_webhook_key  # noqa: E402


# Los modelos usan JSONB (postgres-only) en algunas columnas (ej. metadata de
# AuditLog, shipping_line/order_applied_discount de QuoteRequest). SQLite no
# conoce ese tipo — se mapea a JSON genérico solo para el dialecto sqlite del
# test, sin tocar el comportamiento real contra Postgres.
@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


# ---------------------------------------------------------------------------
# Infra de test — BD SQLite en memoria en vez de la BD real (DATABASE_URL).
# ---------------------------------------------------------------------------
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
def existing_company(db_session_factory) -> str:
    """Empresa ya registrada en Apro Click, con Shopify Company vinculada."""
    with db_session_factory() as session:
        company = Company(
            id=uuid_mod.uuid4(),
            name="DSM NUTRITIONAL PRODUCTS CHILE S.A",
            billing_rut="969594706",
            billing_razon_social="DSM NUTRITIONAL PRODUCTS CHILE S.A",
            shopify_company_id="gid://shopify/Company/999",
        )
        session.add(company)
        session.commit()
        return str(company.id)


@pytest.fixture()
def quote_with_apro_ia(db_session_factory) -> str:
    """Crea una QuoteRequest ya sincronizada con APRO IA (folio CO-000040)."""
    with db_session_factory() as session:
        company = Company(
            id=uuid_mod.uuid4(),
            name="DSM NUTRITIONAL PRODUCTS CHILE S.A",
            billing_rut="969594706",
            billing_razon_social="DSM NUTRITIONAL PRODUCTS CHILE S.A",
        )
        session.add(company)
        quote = QuoteRequest(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            shopify_company_id="gid://shopify/Company/1",
            shop_domain="aproclick.myshopify.com",
            folio="CO-000040",
            apro_ia_id="6a3931c8c5504d51bd5c5bb9",
            apro_ia_folio="CO-000040",
            status="COTIZADA",
            draft_order_gid="gid://shopify/DraftOrder/1",
        )
        quote.lines.append(
            QuoteRequestLine(
                id=uuid_mod.uuid4(),
                line_number=1,
                variant_gid="gid://shopify/ProductVariant/1",
                sku="00100006979000",
                title="BARRA CONECTORA P/CONOS BLANCA/ROJA GREENWAY",
                quantity=1,
                negotiated_unit_price=Decimal("1300"),
            )
        )
        session.add(quote)
        session.flush()
        quote_id = str(quote.id)
        session.commit()
        return quote_id


# Payload real de ejemplo documentado en docs/Webhook eventos.md /
# docs/COTIZACIONES APRO IA.pdf (misma cotización CO-000040).
def _apro_ia_payload(event: str, status: str, **overrides) -> dict:
    payload = {
        "event": event,
        "folio": "CO-000040",
        "id": "6a3931c8c5504d51bd5c5bb9",
        "status": status,
        "client": {"name": "DSM NUTRITIONAL PRODUCTS CHILE S.A", "rut": "969594706"},
        "seller": {"name": "Vania Vergara", "email": "vania.vergara@apro.cl"},
        "products": [
            {
                "sku": "00100006979000",
                "name": "BARRA CONECTORA P/CONOS BLANCA/ROJA GREENWAY",
                "amount": 1,
                "price": 1300,
                "total": 1300,
            }
        ],
        "comment": "comentario de prueba",
        "net_amount": 3613,
        "iva_amount": 687,
        "total_amount": 4300,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# quote_service.handle_apro_ia_webhook — lógica de negocio
# ---------------------------------------------------------------------------
def test_rechaza_evento_desconocido(db_session_factory):
    with pytest.raises(ValueError):
        quote_service.handle_apro_ia_webhook(
            _apro_ia_payload("quotation.deleted", "Rechazada")
        )


def test_requiere_folio_o_id(db_session_factory):
    payload = _apro_ia_payload("quotation.updated", "Pendiente")
    payload["folio"] = ""
    payload["id"] = ""
    with pytest.raises(ValueError):
        quote_service.handle_apro_ia_webhook(payload)


def test_sin_cotizacion_coincidente_no_falla(db_session_factory):
    payload = _apro_ia_payload("quotation.updated", "Pendiente")
    payload["folio"] = "CO-999999"
    payload["id"] = "no-existe"
    result = quote_service.handle_apro_ia_webhook(payload)
    assert result == {"received": True, "matched": False}


# ---------------------------------------------------------------------------
# Folio reusado por APRO IA entre cotizaciones distintas — caso real: folio
# AC-000007 reportado para C3VM SPA cuando ya existía una QuoteRequest con
# ese mismo folio para CO STUDIO SPA (apro_ia_id distinto). El matching debe
# usar apro_ia_id, no folio, o termina actualizando la cotización del cliente
# equivocado.
# ---------------------------------------------------------------------------
def test_folio_duplicado_no_actualiza_cotizacion_de_otro_cliente(
    quote_with_apro_ia, db_session_factory, monkeypatch
):
    with db_session_factory() as session:
        otra_empresa = Company(
            id=uuid_mod.uuid4(),
            name="C3VM SPA",
            billing_rut="77650579-K",
            billing_razon_social="C3VM SPA",
            shopify_company_id="gid://shopify/Company/3757801656",
        )
        session.add(otra_empresa)
        session.commit()

    monkeypatch.setattr(
        quote_service,
        "resolve_store_token",
        lambda session, shop_domain=None: ("aproclick.myshopify.com", "fake-token"),
    )
    monkeypatch.setattr(
        quote_service.draft_order_service,
        "resolve_variant_by_sku",
        lambda shop_domain, token, sku: {
            "id": f"gid://shopify/ProductVariant/{sku}",
            "sku": sku,
            "displayName": f"Producto {sku}",
        },
    )

    def _fake_create_draft(session, quote):
        quote.draft_order_gid = "gid://shopify/DraftOrder/2"
        return {"id": quote.draft_order_gid}

    monkeypatch.setattr(
        quote_service.draft_order_service, "create_quote_draft", _fake_create_draft
    )
    monkeypatch.setattr(
        quote_service.apro_ia_client,
        "descargar_pdf",
        lambda folio: (_ for _ in ()).throw(RuntimeError("sin PDF en test")),
    )
    monkeypatch.setattr(
        quote_service, "encolar_nueva_cotizacion", lambda **kw: None
    )

    payload = _apro_ia_payload("quotation.created", "Pendiente")
    payload["folio"] = "CO-000040"  # mismo folio que quote_with_apro_ia
    payload["id"] = "otro-id-distinto"
    payload["client"] = {"name": "C3VM SPA", "rut": "77650579K"}

    result = quote_service.handle_apro_ia_webhook(payload)

    # Debe crear una QuoteRequest nueva (no matchear la existente por folio).
    assert result["matched"] is False
    assert result["created"] is True

    with db_session_factory() as session:
        original = session.get(QuoteRequest, uuid_mod.UUID(quote_with_apro_ia))
        assert original.apro_ia_id == "6a3931c8c5504d51bd5c5bb9"
        # La cotización original no fue tocada por el evento del otro cliente.
        assert len(original.lines) == 1
        assert original.lines[0].sku == "00100006979000"

        new_quote = session.get(QuoteRequest, uuid_mod.UUID(result["quote_id"]))
        assert new_quote.apro_ia_id == "otro-id-distinto"
        assert new_quote.apro_ia_folio == "CO-000040"
        # folio propio es único: se desambigua porque "CO-000040" ya lo usa
        # la cotización original.
        assert new_quote.folio == "CO-000040-stinto"


# ---------------------------------------------------------------------------
# quotation.created sin contraparte — cotización originada directo en APRO IA
# (caso real: folio CO-076038, ver hallazgo en CloudWatch del 2026-07-06).
# ---------------------------------------------------------------------------
def test_created_sin_empresa_registrada_no_crea_y_avisa_admins(db_session_factory, monkeypatch):
    """Si el RUT del cliente no matchea ninguna Company, no se inventa nada:
    se omite la creación y se notifica a los admins para alta manual."""
    sent = []
    monkeypatch.setattr(
        quote_service, "encolar_cotizacion_apro_ia_estado", lambda **kw: sent.append(kw)
    )
    payload = _apro_ia_payload("quotation.created", "Pendiente")
    payload["folio"] = "CO-076038"
    payload["id"] = "6a4bdab613207653d69e7567"
    payload["client"] = {"name": "Empresa Sin Registrar SPA", "rut": "11111111-1"}

    result = quote_service.handle_apro_ia_webhook(payload)

    assert result == {"received": True, "matched": False, "created": False}
    assert len(sent) == 1
    assert sent[0]["event"] == "quotation.created"
    assert "no existe en Apro Click" in sent[0]["status"]


def test_created_con_empresa_crea_cotizacion_y_draft_order(
    existing_company, db_session_factory, monkeypatch
):
    """Empresa existente (RUT matchea) + SKUs resolubles en Shopify → crea la
    QuoteRequest, resuelve las líneas y deja el Draft Order armado."""
    monkeypatch.setattr(
        quote_service,
        "resolve_store_token",
        lambda session, shop_domain=None: ("aproclick.myshopify.com", "fake-token"),
    )
    monkeypatch.setattr(
        quote_service.draft_order_service,
        "resolve_variant_by_sku",
        lambda shop_domain, token, sku: {
            "id": f"gid://shopify/ProductVariant/{sku}",
            "sku": sku,
            "displayName": f"Producto {sku}",
        },
    )

    def _fake_create_draft(session, quote):
        quote.draft_order_gid = "gid://shopify/DraftOrder/1"
        return {"id": quote.draft_order_gid}

    monkeypatch.setattr(
        quote_service.draft_order_service, "create_quote_draft", _fake_create_draft
    )

    def _sin_pdf(folio):
        raise RuntimeError("APRO IA no tiene PDF en este test")

    monkeypatch.setattr(quote_service.apro_ia_client, "descargar_pdf", _sin_pdf)

    notificados = []
    monkeypatch.setattr(
        quote_service, "encolar_nueva_cotizacion", lambda **kw: notificados.append(kw)
    )

    payload = _apro_ia_payload("quotation.created", "Pendiente")
    payload["folio"] = "CO-076038"
    payload["id"] = "6a4bdab613207653d69e7567"

    result = quote_service.handle_apro_ia_webhook(payload)

    assert result["matched"] is False
    assert result["created"] is True
    assert result["status"] == "Pendiente"
    assert len(notificados) == 1
    assert notificados[0]["line_count"] == 1

    with db_session_factory() as session:
        created = session.get(QuoteRequest, uuid_mod.UUID(result["quote_id"]))
        assert created is not None
        assert created.company_id == uuid_mod.UUID(existing_company)
        assert created.apro_ia_folio == "CO-076038"
        assert created.folio == "CO-076038"
        assert created.status == "COTIZADA"  # el draft se creó sin errores
        assert created.draft_order_gid == "gid://shopify/DraftOrder/1"
        assert len(created.lines) == 1
        assert created.lines[0].sku == "00100006979000"


def test_created_con_empresa_pero_sku_no_encontrado_no_crea(
    existing_company, db_session_factory, monkeypatch
):
    """Si ningún SKU del payload resuelve en Shopify, no se crea una
    cotización sin líneas."""
    monkeypatch.setattr(
        quote_service,
        "resolve_store_token",
        lambda session, shop_domain=None: ("aproclick.myshopify.com", "fake-token"),
    )
    monkeypatch.setattr(
        quote_service.draft_order_service,
        "resolve_variant_by_sku",
        lambda shop_domain, token, sku: None,
    )
    payload = _apro_ia_payload("quotation.created", "Pendiente")
    payload["folio"] = "CO-076038"
    payload["id"] = "6a4bdab613207653d69e7567"

    result = quote_service.handle_apro_ia_webhook(payload)

    assert result == {"received": True, "matched": False, "created": False}


def test_created_matchea_empresa_con_rut_en_formato_distinto(
    db_session_factory, monkeypatch
):
    """Regresión del caso real folio CO-076039: la empresa existe con
    ``billing_rut`` guardado con guión (``76217505-3``), pero APRO IA envía el
    RUT del webhook sin separadores (``762175053``). Debe matchear igual."""
    with db_session_factory() as session:
        company = Company(
            id=uuid_mod.uuid4(),
            name="JULIO EDUARDO VARAS ESPINOZA CONSTRUCCIONES E.I.R.L",
            billing_rut="76217505-3",
            shopify_company_id="gid://shopify/Company/1234",
        )
        session.add(company)
        session.flush()
        company_id = company.id
        session.commit()

    monkeypatch.setattr(
        quote_service,
        "resolve_store_token",
        lambda session, shop_domain=None: ("aproclick.myshopify.com", "fake-token"),
    )
    monkeypatch.setattr(
        quote_service.draft_order_service,
        "resolve_variant_by_sku",
        lambda shop_domain, token, sku: {
            "id": f"gid://shopify/ProductVariant/{sku}",
            "sku": sku,
            "displayName": f"Producto {sku}",
        },
    )
    monkeypatch.setattr(
        quote_service.draft_order_service,
        "create_quote_draft",
        lambda session, quote: quote.__setattr__(
            "draft_order_gid", "gid://shopify/DraftOrder/2"
        ),
    )
    monkeypatch.setattr(
        quote_service.apro_ia_client,
        "descargar_pdf",
        lambda folio: (_ for _ in ()).throw(RuntimeError("sin PDF en el test")),
    )
    monkeypatch.setattr(quote_service, "encolar_nueva_cotizacion", lambda **kw: None)

    payload = _apro_ia_payload("quotation.created", "Pendiente")
    payload["folio"] = "CO-076039"
    payload["id"] = "6a4beea013207653d69e75b4"
    payload["client"] = {
        "name": "JULIO EDUARDO VARAS ESPINOZA CONSTRUCCIONES E.I.R.L",
        "rut": "762175053",
    }

    result = quote_service.handle_apro_ia_webhook(payload)

    assert result["created"] is True
    with db_session_factory() as session:
        created = session.get(QuoteRequest, uuid_mod.UUID(result["quote_id"]))
        assert created.company_id == company_id
        assert created.status == "COTIZADA"


def test_created_y_updated_persisten_estado_sin_notificar(quote_with_apro_ia, monkeypatch):
    sent = []
    monkeypatch.setattr(
        quote_service, "encolar_cotizacion_apro_ia_estado", lambda **kw: sent.append(kw)
    )

    for event, status in (
        ("quotation.created", "Pendiente"),
        ("quotation.updated", "Pendiente Gerente Comercial"),
    ):
        result = quote_service.handle_apro_ia_webhook(_apro_ia_payload(event, status))
        assert result["matched"] is True
        assert result["quote_id"] == quote_with_apro_ia
        assert result["status"] == status

    assert sent == []  # created/updated nunca notifican a SALES


def test_approved_notifica_una_sola_vez_ante_reintentos(quote_with_apro_ia, monkeypatch):
    sent = []
    monkeypatch.setattr(
        quote_service, "encolar_cotizacion_apro_ia_estado", lambda **kw: sent.append(kw)
    )
    payload = _apro_ia_payload("quotation.approved", "Autorizada")

    result = quote_service.handle_apro_ia_webhook(payload)
    assert result["matched"] is True
    assert result["quote_id"] == quote_with_apro_ia
    assert len(sent) == 1
    assert sent[0]["event"] == "quotation.approved"
    assert sent[0]["status"] == "Autorizada"
    assert sent[0]["folio"] == "CO-000040"

    # Reintento del webhook con el mismo estado: no debe reenviar el aviso.
    result2 = quote_service.handle_apro_ia_webhook(payload)
    assert result2["matched"] is True
    assert len(sent) == 1


def test_rejected_notifica_a_sales(quote_with_apro_ia, monkeypatch):
    sent = []
    monkeypatch.setattr(
        quote_service, "encolar_cotizacion_apro_ia_estado", lambda **kw: sent.append(kw)
    )
    payload = _apro_ia_payload(
        "quotation.rejected", "Rechazada", comment_auth="Precio fuera de política"
    )
    result = quote_service.handle_apro_ia_webhook(payload)
    assert result["matched"] is True
    assert len(sent) == 1
    assert sent[0]["event"] == "quotation.rejected"
    assert sent[0]["status"] == "Rechazada"


def test_approved_sincroniza_cantidad_y_precio_y_actualiza_draft(
    quote_with_apro_ia, db_session_factory, monkeypatch
):
    """Regresión del caso real folio CO-076041: el Gerente Comercial subió la
    cantidad de 1 a 5 y el precio de 1300 a 20000 al aprobar en APRO IA — eso
    debe reflejarse en QuoteRequestLine y empujarse al Draft Order existente."""
    monkeypatch.setattr(
        quote_service, "encolar_cotizacion_apro_ia_estado", lambda **kw: None
    )
    draft_updates = []
    monkeypatch.setattr(
        quote_service.draft_order_service,
        "update_quote_draft",
        lambda session, quote: draft_updates.append(str(quote.id)) or {},
    )

    payload = _apro_ia_payload("quotation.approved", "Autorizada")
    payload["products"][0]["amount"] = 5
    payload["products"][0]["price"] = 20000
    payload["products"][0]["total"] = 100000

    result = quote_service.handle_apro_ia_webhook(payload)

    assert result["matched"] is True
    assert len(draft_updates) == 1
    assert draft_updates[0] == quote_with_apro_ia

    with db_session_factory() as session:
        updated = session.get(QuoteRequest, uuid_mod.UUID(quote_with_apro_ia))
        assert updated.apro_ia_status == "Autorizada"
        assert len(updated.lines) == 1
        assert updated.lines[0].quantity == 5
        assert updated.lines[0].negotiated_unit_price == Decimal("20000")


def test_updated_sin_draft_order_no_intenta_actualizar_shopify(
    db_session_factory, monkeypatch
):
    """Si la cotización todavía no tiene Draft Order (ej. quedó en SOLICITADA
    porque el draft inicial falló), sincronizar líneas no debe intentar
    actualizar Shopify — solo queda persistido localmente."""
    with db_session_factory() as session:
        company = Company(
            id=uuid_mod.uuid4(),
            name="DSM NUTRITIONAL PRODUCTS CHILE S.A",
            billing_rut="969594706",
        )
        session.add(company)
        quote = QuoteRequest(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            shopify_company_id="gid://shopify/Company/1",
            shop_domain="aproclick.myshopify.com",
            folio="CO-000041",
            apro_ia_id="abc123",
            apro_ia_folio="CO-000041",
            status="SOLICITADA",
        )
        quote.lines.append(
            QuoteRequestLine(
                line_number=1,
                variant_gid="gid://shopify/ProductVariant/1",
                sku="00100006979000",
                title="BARRA CONECTORA P/CONOS BLANCA/ROJA GREENWAY",
                quantity=1,
                negotiated_unit_price=Decimal("1300"),
            )
        )
        session.add(quote)
        session.commit()

    draft_updates = []
    monkeypatch.setattr(
        quote_service.draft_order_service,
        "update_quote_draft",
        lambda session, quote: draft_updates.append(str(quote.id)) or {},
    )
    payload = _apro_ia_payload("quotation.updated", "Pendiente")
    payload["folio"] = "CO-000041"
    payload["id"] = "abc123"
    payload["products"][0]["amount"] = 3

    result = quote_service.handle_apro_ia_webhook(payload)

    assert result["matched"] is True
    assert draft_updates == []
    with db_session_factory() as session:
        updated = session.get(QuoteRequest, uuid_mod.UUID(result["quote_id"]))
        assert updated.lines[0].quantity == 3


# ---------------------------------------------------------------------------
# utils.quotes_api_key.require_apro_ia_webhook_key — autenticación
# ---------------------------------------------------------------------------
def test_webhook_key_rechaza_si_no_configurada(monkeypatch):
    monkeypatch.delenv("APRO_IA_WEBHOOK_API_KEY", raising=False)
    with pytest.raises(PermissionError):
        require_apro_ia_webhook_key({"x-api-key": "cualquiera"})


def test_webhook_key_rechaza_clave_incorrecta(monkeypatch):
    monkeypatch.setenv("APRO_IA_WEBHOOK_API_KEY", "clave-correcta")
    with pytest.raises(PermissionError):
        require_apro_ia_webhook_key({"x-api-key": "clave-incorrecta"})


def test_webhook_key_acepta_header_case_insensitive(monkeypatch):
    monkeypatch.setenv("APRO_IA_WEBHOOK_API_KEY", "clave-correcta")
    require_apro_ia_webhook_key({"X-Api-Key": "clave-correcta"})  # no lanza


# ---------------------------------------------------------------------------
# Endpoint HTTP completo — simula exactamente el POST que enviaría APRO IA.
# ---------------------------------------------------------------------------
class _FakeLambdaContext:
    function_name = "apro-click-admin-quotes-dev-quotes"
    memory_limit_in_mb = 512
    invoked_function_arn = (
        "arn:aws:lambda:us-east-2:000000000000:function:quotes"
    )
    aws_request_id = "00000000-0000-0000-0000-000000000000"


def _api_gateway_event(body: dict, headers: dict) -> dict:
    """Evento API Gateway HTTP API (payload v2), igual al que recibe la Lambda."""
    return {
        "version": "2.0",
        "routeKey": "POST /api/v1/quotes/webhooks/apro-ia",
        "rawPath": "/api/v1/quotes/webhooks/apro-ia",
        "rawQueryString": "",
        "headers": headers,
        "requestContext": {
            "http": {
                "method": "POST",
                "path": "/api/v1/quotes/webhooks/apro-ia",
                "protocol": "HTTP/1.1",
                "sourceIp": "203.0.113.10",
            },
            "domainName": "90cle868eb.execute-api.us-east-2.amazonaws.com",
            "stage": "$default",
            "requestId": "test-request-id",
        },
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }


def test_endpoint_rechaza_sin_api_key(monkeypatch):
    monkeypatch.delenv("APRO_IA_WEBHOOK_API_KEY", raising=False)
    event = _api_gateway_event(
        _apro_ia_payload("quotation.approved", "Autorizada"),
        headers={"content-type": "application/json"},
    )
    response = quotes_handler.lambda_handler(event, _FakeLambdaContext())
    assert response["statusCode"] == 401


def test_endpoint_rechaza_api_key_incorrecta(monkeypatch):
    monkeypatch.setenv("APRO_IA_WEBHOOK_API_KEY", "clave-correcta")
    event = _api_gateway_event(
        _apro_ia_payload("quotation.approved", "Autorizada"),
        headers={"content-type": "application/json", "x-api-key": "clave-incorrecta"},
    )
    response = quotes_handler.lambda_handler(event, _FakeLambdaContext())
    assert response["statusCode"] == 401


def test_endpoint_procesa_payload_documentado(
    monkeypatch, quote_with_apro_ia, db_session_factory
):
    monkeypatch.setenv("APRO_IA_WEBHOOK_API_KEY", "clave-correcta")
    sent = []
    monkeypatch.setattr(
        quote_service, "encolar_cotizacion_apro_ia_estado", lambda **kw: sent.append(kw)
    )
    payload = _apro_ia_payload("quotation.rejected", "Rechazada")
    event = _api_gateway_event(
        payload,
        headers={"content-type": "application/json", "x-api-key": "clave-correcta"},
    )

    response = quotes_handler.lambda_handler(event, _FakeLambdaContext())

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["data"]["matched"] is True
    assert body["data"]["quote_id"] == quote_with_apro_ia
    assert len(sent) == 1
    assert sent[0]["event"] == "quotation.rejected"


def test_endpoint_rechaza_body_invalido(monkeypatch):
    monkeypatch.setenv("APRO_IA_WEBHOOK_API_KEY", "clave-correcta")
    event = _api_gateway_event({}, headers={"content-type": "application/json", "x-api-key": "clave-correcta"})
    event["body"] = "no-es-json"
    response = quotes_handler.lambda_handler(event, _FakeLambdaContext())
    assert response["statusCode"] == 400


# ---------------------------------------------------------------------------
# Envío como línea «COSTO DE ENVIO» y comentario por línea, desde APRO IA
# ---------------------------------------------------------------------------
_ENVIO_SKU = quote_service.apro_ia_client.DEFAULT_SHIPPING_SKU


def _producto_envio(price: int, name: str = "COSTO DE ENVIO - Starken") -> dict:
    return {"sku": _ENVIO_SKU, "name": name, "amount": 1, "price": price, "total": price}


def _sin_shopify(*a, **kw):
    raise AssertionError("el producto de envío no debe buscarse en el catálogo")


def test_webhook_actualiza_precio_del_envio_y_el_draft(
    quote_with_apro_ia, db_session_factory, monkeypatch
):
    with db_session_factory() as session:
        quote = session.get(QuoteRequest, uuid_mod.UUID(quote_with_apro_ia))
        quote.shipping_line = {"title": "Starken", "price": "3990"}
        session.commit()
    monkeypatch.setattr(quote_service.draft_order_service, "resolve_variant_by_sku", _sin_shopify)
    drafts = []
    monkeypatch.setattr(
        quote_service.draft_order_service,
        "update_quote_draft",
        lambda session, quote: drafts.append(dict(quote.shipping_line)),
    )

    payload = _apro_ia_payload("quotation.updated", "Pendiente")
    payload["products"].append(_producto_envio(5000))
    quote_service.handle_apro_ia_webhook(payload)

    assert drafts == [{"title": "Starken", "price": "5000"}]
    with db_session_factory() as session:
        quote = session.get(QuoteRequest, uuid_mod.UUID(quote_with_apro_ia))
        assert quote.shipping_line == {"title": "Starken", "price": "5000"}
        assert len(quote.lines) == 1  # el envío no se vuelve línea de catálogo


def test_webhook_sin_producto_de_envio_no_borra_el_envio(
    quote_with_apro_ia, db_session_factory, monkeypatch
):
    """Cotizaciones anteriores al cambio no tienen el envío en APRO IA."""
    with db_session_factory() as session:
        quote = session.get(QuoteRequest, uuid_mod.UUID(quote_with_apro_ia))
        quote.shipping_line = {"title": "Starken", "price": "3990"}
        session.commit()
    drafts = []
    monkeypatch.setattr(
        quote_service.draft_order_service,
        "update_quote_draft",
        lambda session, quote: drafts.append(1),
    )

    quote_service.handle_apro_ia_webhook(_apro_ia_payload("quotation.updated", "Pendiente"))

    assert drafts == []
    with db_session_factory() as session:
        quote = session.get(QuoteRequest, uuid_mod.UUID(quote_with_apro_ia))
        assert quote.shipping_line == {"title": "Starken", "price": "3990"}


def test_webhook_guarda_comentario_de_linea_sin_tocar_el_draft(
    quote_with_apro_ia, db_session_factory, monkeypatch
):
    drafts = []
    monkeypatch.setattr(
        quote_service.draft_order_service,
        "update_quote_draft",
        lambda session, quote: drafts.append(1),
    )

    payload = _apro_ia_payload("quotation.updated", "Pendiente")
    payload["products"][0]["comment_product"] = "  Entrega en 10 días  "
    quote_service.handle_apro_ia_webhook(payload)

    assert drafts == []
    with db_session_factory() as session:
        quote = session.get(QuoteRequest, uuid_mod.UUID(quote_with_apro_ia))
        assert quote.lines[0].comment == "Entrega en 10 días"


def test_created_toma_envio_y_comentarios_de_apro_ia(
    existing_company, db_session_factory, monkeypatch
):
    monkeypatch.setattr(
        quote_service,
        "resolve_store_token",
        lambda session, shop_domain=None: ("aproclick.myshopify.com", "fake-token"),
    )

    def _variant(shop_domain, token, sku):
        assert sku != _ENVIO_SKU, "el producto de envío no debe buscarse en el catálogo"
        return {"id": f"gid://shopify/ProductVariant/{sku}", "sku": sku, "displayName": sku}

    monkeypatch.setattr(quote_service.draft_order_service, "resolve_variant_by_sku", _variant)
    monkeypatch.setattr(
        quote_service.draft_order_service, "create_quote_draft", lambda session, quote: {}
    )
    monkeypatch.setattr(
        quote_service.apro_ia_client,
        "descargar_pdf",
        lambda folio: (_ for _ in ()).throw(RuntimeError("sin PDF")),
    )
    monkeypatch.setattr(quote_service, "encolar_nueva_cotizacion", lambda **kw: None)

    payload = _apro_ia_payload("quotation.created", "Pendiente", id="nuevo-1", folio="AC-1")
    payload["products"][0]["comment_product"] = "Color rojo"
    payload["products"].append(_producto_envio(4500, name="COSTO DE ENVIO"))
    result = quote_service.handle_apro_ia_webhook(payload)

    with db_session_factory() as session:
        quote = session.get(QuoteRequest, uuid_mod.UUID(result["quote_id"]))
        assert [ln.sku for ln in quote.lines] == ["00100006979000"]
        assert quote.lines[0].comment == "Color rojo"
        assert quote.shipping_line == {"title": "Envío", "price": "4500"}
