"""Tests del webhook de transferencias de APRO IA.

Payload documentado en ``docs/Transferencias AproClick.pdf`` (``folio``,
``rut``, ``status``, ``order``) y comportamiento en
``docs/Transferencias APRO IA.md``.

Cubren ``quote_service.handle_apro_ia_transfer_webhook`` con una BD SQLite en
memoria y el endpoint HTTP completo
(``POST /api/v1/quotes/webhooks/apro-ia/transferencias``) simulando el evento
API Gateway HTTP API v2. No hacen llamadas reales a Shopify ni a SQS: se
monkeypatchean ``draft_order_service`` y ``encolar_transferencia_estado``.

Ejecutar con:
  cd backend-v1 && python3 -m pytest src/services/quotes/tests/test_apro_ia_transferencias.py -v
"""
from __future__ import annotations

import json
import sys
import uuid as uuid_mod
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
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
from database.models.client import Client  # noqa: E402
from database.models.company import Company  # noqa: E402
from database.models.quote_request import QuoteRequest, QuoteRequestLine  # noqa: E402
from database.models.shopify_order import ShopifyOrder  # noqa: E402

from handlers import quotes as quotes_handler  # noqa: E402
from services import quote_service  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


# ---------------------------------------------------------------------------
# Infra de test
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


@pytest.fixture(autouse=True)
def sin_emails(monkeypatch):
    """Captura los avisos a SALES en vez de encolarlos en SQS."""
    enviados: list[dict] = []
    monkeypatch.setattr(
        quote_service,
        "encolar_transferencia_estado",
        lambda **kwargs: enviados.append(kwargs),
    )
    return enviados


@pytest.fixture(autouse=True)
def avisos_cliente(monkeypatch):
    """Captura los avisos al cliente (pago con crédito) en vez de encolarlos."""
    enviados: list[dict] = []
    monkeypatch.setattr(
        quote_service,
        "encolar_pago_credito_cliente",
        lambda **kwargs: enviados.append(kwargs),
    )
    return enviados


class _ShopifyFake:
    """Registra las llamadas que el servicio hace a ``draft_order_service``."""

    def __init__(self) -> None:
        self.llamadas: list[str] = []
        self.medios: list[str] = []
        self.notas_anulacion: list[str] = []
        self.rechazos: list[dict] = []
        self.order_ya_pagado = False

    def create_quote_draft(self, session, quote):
        self.llamadas.append("create_quote_draft")
        quote.draft_order_gid = "gid://shopify/DraftOrder/nuevo"
        return {"id": quote.draft_order_gid}

    def complete_quote(self, session, quote, *, payment_pending=True):
        self.llamadas.append(f"complete_quote(payment_pending={payment_pending})")
        quote.order_gid = "gid://shopify/Order/555"
        return {"id": quote.draft_order_gid, "order": {"id": quote.order_gid}}

    def mark_order_paid(
        self,
        session,
        order_gid,
        *,
        shop_domain="",
        transfer_folio="",
        payment_method="transferencia",
    ):
        self.llamadas.append(f"mark_order_paid({transfer_folio})")
        self.medios.append(payment_method)
        return {"order": {"id": order_gid}, "already_paid": self.order_ya_pagado}

    def tag_order_payment_rejected(
        self,
        session,
        order_gid,
        *,
        shop_domain="",
        transfer_folio="",
        payment_method="transferencia",
        raw_status="",
    ):
        self.llamadas.append("tag_order_payment_rejected")
        self.rechazos.append(
            {
                "order_gid": order_gid,
                "transfer_folio": transfer_folio,
                "payment_method": payment_method,
                "raw_status": raw_status,
            }
        )
        return {
            "order": {"id": order_gid},
            "already_tagged": False,
            "was_paid": self.order_ya_pagado,
        }

    def cancel_order(self, session, order_gid, *, shop_domain="", staff_note=""):
        # El webhook ya no debe anular pedidos: si alguien la llama, el test lo
        # ve en `llamadas` y falla la aserción correspondiente.
        self.llamadas.append("cancel_order")
        self.notas_anulacion.append(staff_note)
        return {"job": {"id": "gid://shopify/Job/1"}, "already_cancelled": False}

    def delete_quote_draft(self, session, quote):
        self.llamadas.append("delete_quote_draft")
        quote.draft_order_gid = None
        return {"deletedId": "gid://shopify/DraftOrder/1"}


@pytest.fixture()
def shopify(monkeypatch) -> _ShopifyFake:
    fake = _ShopifyFake()
    monkeypatch.setattr(quote_service, "draft_order_service", fake)
    return fake


def _crear_shopify_order(
    session,
    *,
    company_id,
    shopify_order_id: str = "555",
    order_name: str = "#1001",
) -> None:
    """Pedido sincronizado por el webhook ``orders/create`` de Shopify."""
    session.add(
        ShopifyOrder(
            id=uuid_mod.uuid4(),
            shopify_order_id=shopify_order_id,
            shop_domain="aproclick.myshopify.com",
            company_id=company_id,
            order_name=order_name,
            financial_status="pending",
        )
    )


def _crear_quote(
    db_session_factory,
    *,
    folio: str = "CO-000040",
    apro_ia_folio: str | None = "CO-000040",
    rut: str = "76217505-3",
    status: str = "COTIZADA",
    draft_order_gid: str | None = "gid://shopify/DraftOrder/1",
    order_gid: str | None = None,
    company_name: str = "LABOR SERVICES SPA",
    order_name: str = "#1001",
) -> str:
    """Cotización de prueba. Si tiene pedido, también se sincroniza el `ShopifyOrder`."""
    with db_session_factory() as session:
        company = Company(
            id=uuid_mod.uuid4(),
            name=company_name,
            billing_rut=rut,
            billing_razon_social=company_name,
            shopify_company_id="gid://shopify/Company/1",
        )
        session.add(company)
        quote = QuoteRequest(
            id=uuid_mod.uuid4(),
            company_id=company.id,
            shopify_company_id="gid://shopify/Company/1",
            shop_domain="aproclick.myshopify.com",
            folio=folio,
            apro_ia_folio=apro_ia_folio,
            apro_ia_id=f"id-{folio}",
            status=status,
            draft_order_gid=draft_order_gid,
            order_gid=order_gid,
        )
        quote.lines.append(
            QuoteRequestLine(
                id=uuid_mod.uuid4(),
                line_number=1,
                variant_gid="gid://shopify/ProductVariant/1",
                sku="00100006979000",
                title="BARRA CONECTORA",
                quantity=1,
                negotiated_unit_price=Decimal("1300"),
            )
        )
        session.add(quote)
        if order_gid:
            _crear_shopify_order(
                session,
                company_id=company.id,
                shopify_order_id=order_gid.rsplit("/", 1)[-1],
                order_name=order_name,
            )
        session.flush()
        quote_id = str(quote.id)
        session.commit()
        return quote_id


def _crear_pedido_sin_cotizacion(
    db_session_factory,
    *,
    rut: str = "76217505-3",
    shopify_order_id: str = "777",
    order_name: str = "#1002",
) -> None:
    """Pedido B2B que salió del checkout sin pasar por una cotización nuestra."""
    with db_session_factory() as session:
        company = Company(
            id=uuid_mod.uuid4(),
            name="LABOR SERVICES SPA",
            billing_rut=rut,
            billing_razon_social="LABOR SERVICES SPA",
            shopify_company_id="gid://shopify/Company/1",
        )
        session.add(company)
        session.flush()
        _crear_shopify_order(
            session,
            company_id=company.id,
            shopify_order_id=shopify_order_id,
            order_name=order_name,
        )
        session.commit()


def _payload(status: str, **overrides) -> dict:
    """Payload real: ``order`` es el ID del pedido de Shopify."""
    payload = {
        "folio": "TR-000123",
        "rut": "762175053",
        "status": status,
        "order": "555",
    }
    payload.update(overrides)
    return payload


def _leer(db_session_factory, quote_id: str) -> QuoteRequest:
    with db_session_factory() as session:
        return session.scalar(
            select(QuoteRequest).where(QuoteRequest.id == uuid_mod.UUID(quote_id))
        )


# ---------------------------------------------------------------------------
# Validación de payload
# ---------------------------------------------------------------------------
def test_requiere_order(db_session_factory):
    with pytest.raises(ValueError):
        quote_service.handle_apro_ia_transfer_webhook(_payload("Aprobada", order=""))


def test_requiere_status(db_session_factory):
    with pytest.raises(ValueError):
        quote_service.handle_apro_ia_transfer_webhook(_payload(""))


def test_estado_no_final_se_ignora(db_session_factory, shopify):
    result = quote_service.handle_apro_ia_transfer_webhook(_payload("Pendiente"))
    assert result["applied"] is False
    assert shopify.llamadas == []


@pytest.mark.parametrize("order", ["CO-999999", "999999"])
def test_orden_inexistente_no_falla(db_session_factory, shopify, order):
    result = quote_service.handle_apro_ia_transfer_webhook(
        _payload("Aprobada", order=order)
    )
    assert result == {"received": True, "matched": False, "applied": False}
    assert shopify.llamadas == []


# ---------------------------------------------------------------------------
# Aprobada
# ---------------------------------------------------------------------------
def test_aprobada_con_pedido_existente_solo_marca_pagado(
    db_session_factory, shopify, sin_emails
):
    quote_id = _crear_quote(
        db_session_factory, order_gid="gid://shopify/Order/555", status="ACEPTADA"
    )
    result = quote_service.handle_apro_ia_transfer_webhook(_payload("Aprobada"))

    assert result["applied"] is True
    assert result["quote_id"] == quote_id
    assert shopify.llamadas == ["mark_order_paid(TR-000123)"]

    quote = _leer(db_session_factory, quote_id)
    assert quote.status == "ACEPTADA"
    assert quote.transfer_folio == "TR-000123"
    assert quote.transfer_status == "Aprobada"
    assert quote.transfer_paid_at is not None
    assert len(sin_emails) == 1
    assert sin_emails[0]["estado"] == "APROBADA"


def test_aprobada_sin_pedido_completa_el_draft_impago_y_lo_marca_pagado(
    db_session_factory, shopify
):
    quote_id = _crear_quote(db_session_factory)
    quote_service.handle_apro_ia_transfer_webhook(
        _payload("Aprobada", order="CO-000040")
    )

    assert shopify.llamadas == [
        "complete_quote(payment_pending=True)",
        "mark_order_paid(TR-000123)",
    ]
    quote = _leer(db_session_factory, quote_id)
    assert quote.order_gid == "gid://shopify/Order/555"
    assert quote.status == "ACEPTADA"


def test_aprobada_sin_draft_crea_draft_y_pedido(db_session_factory, shopify):
    quote_id = _crear_quote(
        db_session_factory, draft_order_gid=None, status="SOLICITADA"
    )
    quote_service.handle_apro_ia_transfer_webhook(
        _payload("Aprobada", order="CO-000040")
    )

    assert shopify.llamadas == [
        "create_quote_draft",
        "complete_quote(payment_pending=True)",
        "mark_order_paid(TR-000123)",
    ]
    quote = _leer(db_session_factory, quote_id)
    assert quote.order_gid == "gid://shopify/Order/555"


def test_reenvio_del_mismo_evento_no_vuelve_a_tocar_shopify(
    db_session_factory, shopify, sin_emails
):
    _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")
    quote_service.handle_apro_ia_transfer_webhook(_payload("Aprobada"))
    shopify.llamadas.clear()

    result = quote_service.handle_apro_ia_transfer_webhook(_payload("Aprobada"))
    assert result["applied"] is False
    assert shopify.llamadas == []
    assert len(sin_emails) == 1  # el segundo envío no vuelve a notificar


# ---------------------------------------------------------------------------
# Descartada / En papelera / Anulada / Rechazada — el pedido NO se anula
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status",
    ["Descartada", "En papelera", "Anulada", "descartada / en papelera", "Rechazada"],
)
def test_estados_no_aprobados_etiquetan_el_pedido_sin_anularlo(
    db_session_factory, shopify, status
):
    quote_id = _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")
    quote_service.handle_apro_ia_transfer_webhook(_payload(status))

    assert shopify.llamadas == ["tag_order_payment_rejected"]
    assert shopify.rechazos[0]["payment_method"] == "transferencia"
    assert shopify.rechazos[0]["raw_status"] == status
    quote = _leer(db_session_factory, quote_id)
    assert quote.status == "PAGO_RECHAZADO"
    assert quote.transfer_status == status
    assert quote.transfer_paid_at is None


def test_rechazo_sin_pedido_conserva_el_draft(db_session_factory, shopify):
    quote_id = _crear_quote(db_session_factory)
    result = quote_service.handle_apro_ia_transfer_webhook(
        _payload("Anulada", order="CO-000040")
    )

    assert shopify.llamadas == []
    assert result["applied"] is True
    quote = _leer(db_session_factory, quote_id)
    assert quote.status == "PAGO_RECHAZADO"
    # El Draft Order sigue vivo: es el precio ya negociado y el cliente puede
    # retomar el pago.
    assert quote.draft_order_gid is not None


def test_rechazo_reenviado_no_vuelve_a_tocar_shopify(db_session_factory, shopify):
    _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")

    quote_service.handle_apro_ia_transfer_webhook(_payload("Descartada"))
    result = quote_service.handle_apro_ia_transfer_webhook(_payload("Descartada"))

    assert shopify.llamadas == ["tag_order_payment_rejected"]
    assert result["applied"] is False


# ---------------------------------------------------------------------------
# Formatos de `order` — APRO IA envía el ID del pedido de Shopify
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "order", ["555", "gid://shopify/Order/555", "#1001", "1001"]
)
def test_acepta_los_formatos_de_identificador_de_pedido(
    db_session_factory, shopify, order
):
    """ID pelado, GID completo y número visible del pedido (`#1001`)."""
    quote_id = _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")
    result = quote_service.handle_apro_ia_transfer_webhook(
        _payload("Aprobada", order=order)
    )

    assert result["quote_id"] == quote_id
    assert result["order_gid"] == "gid://shopify/Order/555"
    assert shopify.llamadas == ["mark_order_paid(TR-000123)"]


def test_pedido_no_sincronizado_no_se_procesa(db_session_factory, shopify):
    """Sin el `ShopifyOrder` no hay con qué verificar de quién es el pedido."""
    result = quote_service.handle_apro_ia_transfer_webhook(
        _payload("Aprobada", order="999")
    )
    assert result["matched"] is False
    assert shopify.llamadas == []


# ---------------------------------------------------------------------------
# Pedido del checkout sin cotización asociada
# ---------------------------------------------------------------------------
def test_pedido_sin_cotizacion_igual_queda_pagado(
    db_session_factory, shopify, sin_emails
):
    _crear_pedido_sin_cotizacion(db_session_factory)
    result = quote_service.handle_apro_ia_transfer_webhook(
        _payload("Aprobada", order="777")
    )

    assert result["applied"] is True
    assert result["quote_id"] is None
    assert result["order_gid"] == "gid://shopify/Order/777"
    assert shopify.llamadas == ["mark_order_paid(TR-000123)"]
    assert len(sin_emails) == 1


def test_pedido_sin_cotizacion_ya_pagado_no_notifica(
    db_session_factory, shopify, sin_emails
):
    """Sin cotización la idempotencia la da el propio estado del pedido."""
    _crear_pedido_sin_cotizacion(db_session_factory)
    shopify.order_ya_pagado = True
    result = quote_service.handle_apro_ia_transfer_webhook(
        _payload("Aprobada", order="777")
    )

    assert result["applied"] is False
    assert sin_emails == []


def test_pedido_sin_cotizacion_de_otra_empresa_no_se_toca(
    db_session_factory, shopify
):
    _crear_pedido_sin_cotizacion(db_session_factory, rut="111111111")
    result = quote_service.handle_apro_ia_transfer_webhook(
        _payload("Aprobada", order="777")
    )
    assert result["matched"] is False
    assert shopify.llamadas == []


# ---------------------------------------------------------------------------
# Guarda por RUT — el folio de APRO IA no es único entre cotizaciones
# ---------------------------------------------------------------------------
def test_rut_de_otra_empresa_no_toca_la_cotizacion(db_session_factory, shopify):
    quote_id = _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")
    result = quote_service.handle_apro_ia_transfer_webhook(
        _payload("Aprobada", rut="111111111")
    )

    assert result["matched"] is False
    assert shopify.llamadas == []
    assert _leer(db_session_factory, quote_id).transfer_status is None


def test_folio_duplicado_desambigua_por_rut(db_session_factory, shopify):
    otra = _crear_quote(
        db_session_factory,
        folio="CO-000040",
        rut="111111111",
        company_name="OTRA EMPRESA SPA",
        order_gid="gid://shopify/Order/111",
        order_name="#1003",
    )
    correcta = _crear_quote(
        db_session_factory,
        folio="CO-000040-abc123",
        apro_ia_folio="CO-000040",
        rut="76217505-3",
        order_gid="gid://shopify/Order/555",
    )
    result = quote_service.handle_apro_ia_transfer_webhook(
        _payload("Aprobada", order="CO-000040")
    )

    assert result["quote_id"] == correcta
    assert _leer(db_session_factory, otra).transfer_status is None
    assert _leer(db_session_factory, correcta).transfer_status == "Aprobada"


# ---------------------------------------------------------------------------
# Endpoint HTTP
# ---------------------------------------------------------------------------
def _api_event(
    payload: dict,
    api_key: str,
    path: str = "/api/v1/quotes/webhooks/apro-ia/transferencias",
) -> dict:
    return {
        "version": "2.0",
        "routeKey": f"POST {path}",
        "rawPath": path,
        "headers": {"content-type": "application/json", "x-api-key": api_key},
        "requestContext": {
            "http": {
                "method": "POST",
                "path": path,
                "protocol": "HTTP/1.1",
                "sourceIp": "203.0.113.10",
            },
            "domainName": "90cle868eb.execute-api.us-east-2.amazonaws.com",
            "stage": "$default",
            "requestId": "test-request-id",
        },
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }


def test_endpoint_rechaza_api_key_invalida(db_session_factory, monkeypatch, shopify):
    monkeypatch.setenv("APRO_IA_WEBHOOK_API_KEY", "clave-correcta")
    resp = quotes_handler.lambda_handler(
        _api_event(_payload("Aprobada"), "clave-incorrecta"), None
    )
    assert resp["statusCode"] == 401


def test_endpoint_procesa_transferencia_aprobada(
    db_session_factory, monkeypatch, shopify
):
    monkeypatch.setenv("APRO_IA_WEBHOOK_API_KEY", "clave-correcta")
    quote_id = _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")

    resp = quotes_handler.lambda_handler(
        _api_event(_payload("Aprobada"), "clave-correcta"), None
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["data"]["quote_id"] == quote_id
    assert body["data"]["applied"] is True


def test_fallo_de_shopify_responde_500_para_que_apro_ia_reintente(
    db_session_factory, monkeypatch, shopify
):
    """Un 400 le diría a APRO IA que el payload está mal y que no reintente."""
    monkeypatch.setenv("APRO_IA_WEBHOOK_API_KEY", "clave-correcta")
    _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")

    def _explota(*args, **kwargs):
        raise ValueError("orderMarkAsPaid: Order cannot be marked as paid.")

    monkeypatch.setattr(shopify, "mark_order_paid", _explota)

    resp = quotes_handler.lambda_handler(
        _api_event(_payload("Aprobada"), "clave-correcta"), None
    )
    assert resp["statusCode"] == 500


# ---------------------------------------------------------------------------
# Pagos con crédito — misma ruta lógica, distinto medio de pago
# ---------------------------------------------------------------------------
CREDITOS_PATH = "/api/v1/quotes/webhooks/apro-ia/creditos"


def test_credito_aprobado_marca_pagado_con_el_tag_credito(
    db_session_factory, shopify, sin_emails
):
    quote_id = _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")

    result = quote_service.handle_apro_ia_credit_webhook(_payload("Aprobada"))

    assert result["applied"] is True
    assert shopify.medios == ["credito"]
    quote = _leer(db_session_factory, quote_id)
    assert quote.status == "ACEPTADA"
    assert quote.transfer_paid_at is not None
    assert quote.transfer_folio == "TR-000123"
    assert quote.payment_method == "credito"
    assert sin_emails[0]["payment_method"] == "credito"


def test_transferencia_sigue_marcando_el_medio_transferencia(
    db_session_factory, shopify, sin_emails
):
    """Guarda de no-regresión: el webhook original no cambió de medio."""
    quote_id = _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")

    quote_service.handle_apro_ia_transfer_webhook(_payload("Aprobada"))

    assert shopify.medios == ["transferencia"]
    assert _leer(db_session_factory, quote_id).payment_method == "transferencia"
    assert sin_emails[0]["payment_method"] == "transferencia"


def test_credito_descartado_etiqueta_el_pedido_sin_anularlo(
    db_session_factory, shopify
):
    quote_id = _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")

    result = quote_service.handle_apro_ia_credit_webhook(_payload("Descartada"))

    assert result["applied"] is True
    assert shopify.llamadas == ["tag_order_payment_rejected"]
    assert shopify.rechazos[0]["payment_method"] == "credito"
    assert _leer(db_session_factory, quote_id).status == "PAGO_RECHAZADO"


def test_credito_reenviado_no_vuelve_a_tocar_shopify(db_session_factory, shopify):
    _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")

    quote_service.handle_apro_ia_credit_webhook(_payload("Aprobada"))
    result = quote_service.handle_apro_ia_credit_webhook(_payload("Aprobada"))

    assert result["applied"] is False
    assert shopify.medios == ["credito"]


def test_credito_estado_no_final_se_ignora(db_session_factory, shopify):
    _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")

    result = quote_service.handle_apro_ia_credit_webhook(_payload("Pendiente"))

    assert result["matched"] is False
    assert shopify.llamadas == []


def test_credito_pedido_de_otra_empresa_no_se_toca(db_session_factory, shopify):
    """El RUT es la misma guarda que en transferencias."""
    quote_id = _crear_quote(
        db_session_factory, rut="111111111", order_gid="gid://shopify/Order/555"
    )

    result = quote_service.handle_apro_ia_credit_webhook(_payload("Aprobada"))

    assert result["matched"] is False
    assert shopify.llamadas == []
    assert _leer(db_session_factory, quote_id).payment_method is None


def test_endpoint_creditos_procesa_el_pago(db_session_factory, monkeypatch, shopify):
    monkeypatch.setenv("APRO_IA_WEBHOOK_API_KEY", "clave-correcta")
    quote_id = _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")

    resp = quotes_handler.lambda_handler(
        _api_event(_payload("Aprobada"), "clave-correcta", CREDITOS_PATH), None
    )

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["data"]["quote_id"] == quote_id
    assert body["data"]["applied"] is True
    assert shopify.medios == ["credito"]


def test_endpoint_creditos_rechaza_api_key_invalida(
    db_session_factory, monkeypatch, shopify
):
    monkeypatch.setenv("APRO_IA_WEBHOOK_API_KEY", "clave-correcta")
    resp = quotes_handler.lambda_handler(
        _api_event(_payload("Aprobada"), "clave-incorrecta", CREDITOS_PATH), None
    )
    assert resp["statusCode"] == 401


def test_endpoint_creditos_qa_descarta_rut_fuera_de_la_allowlist(
    db_session_factory, monkeypatch, shopify
):
    """La ruta /qa de créditos hereda la allowlist, como la de transferencias."""
    monkeypatch.setenv("APRO_IA_WEBHOOK_API_KEY", "clave-correcta")
    monkeypatch.setenv("APRO_IA_QA_ALLOWED_RUTS", "76486379-8")
    _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")

    resp = quotes_handler.lambda_handler(
        _api_event(
            _payload("Aprobada"),
            "clave-correcta",
            "/api/v1/quotes/webhooks/apro-ia/qa/creditos",
        ),
        None,
    )

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["data"]["ignored"] is True
    assert shopify.llamadas == []


# ---------------------------------------------------------------------------
# Aviso al cliente del resultado del pago con crédito
# ---------------------------------------------------------------------------
def _con_datos_del_pedido(db_session_factory, *, email: str | None = "compras@labor.cl"):
    with db_session_factory() as session:
        order = session.scalar(select(ShopifyOrder))
        order.email = email
        order.total_price = Decimal("54896")
        order.currency = "CLP"
        session.commit()


def _agregar_contacto(db_session_factory, *, email: str, name: str, principal: bool):
    with db_session_factory() as session:
        company = session.scalar(select(Company))
        session.add(
            Client(
                id=uuid_mod.uuid4(),
                company_id=company.id,
                email=email,
                name=name,
                is_main_contact=principal,
            )
        )
        session.commit()


def test_credito_aprobado_avisa_al_cliente_que_hizo_el_pedido(
    db_session_factory, shopify, avisos_cliente
):
    _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555", order_name="#1128")
    _con_datos_del_pedido(db_session_factory)
    _agregar_contacto(db_session_factory, email="Compras@labor.cl", name="Ana", principal=False)

    quote_service.handle_apro_ia_credit_webhook(_payload("Aprobada"))

    assert avisos_cliente == [
        {
            "contact_email": "compras@labor.cl",
            "contact_name": "Ana",
            "company_name": "LABOR SERVICES SPA",
            "order_name": "#1128",
            "aprobado": True,
            "total": "$54.896 CLP",
        }
    ]


def test_credito_rechazado_avisa_al_cliente(db_session_factory, shopify, avisos_cliente):
    _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")
    _con_datos_del_pedido(db_session_factory)

    quote_service.handle_apro_ia_credit_webhook(_payload("Descartada / En papelera"))

    assert len(avisos_cliente) == 1
    assert avisos_cliente[0]["aprobado"] is False


def test_credito_reenviado_avisa_al_cliente_una_sola_vez(
    db_session_factory, shopify, avisos_cliente
):
    _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")
    _con_datos_del_pedido(db_session_factory)

    quote_service.handle_apro_ia_credit_webhook(_payload("Aprobada"))
    quote_service.handle_apro_ia_credit_webhook(_payload("Aprobada"))

    assert len(avisos_cliente) == 1


def test_credito_de_pedido_ya_pagado_no_avisa_al_cliente(
    db_session_factory, shopify, avisos_cliente
):
    _crear_pedido_sin_cotizacion(db_session_factory, shopify_order_id="555")
    _con_datos_del_pedido(db_session_factory)
    shopify.order_ya_pagado = True

    quote_service.handle_apro_ia_credit_webhook(_payload("Aprobada"))

    assert avisos_cliente == []


def test_transferencia_no_avisa_al_cliente(db_session_factory, shopify, avisos_cliente):
    _crear_quote(db_session_factory, order_gid="gid://shopify/Order/555")
    _con_datos_del_pedido(db_session_factory)

    quote_service.handle_apro_ia_transfer_webhook(_payload("Aprobada"))

    assert avisos_cliente == []


def test_credito_sin_email_en_el_pedido_avisa_al_contacto_principal(
    db_session_factory, shopify, avisos_cliente
):
    _crear_pedido_sin_cotizacion(db_session_factory, shopify_order_id="555")
    _con_datos_del_pedido(db_session_factory, email=None)
    _agregar_contacto(db_session_factory, email="otro@labor.cl", name="Luis", principal=False)
    _agregar_contacto(db_session_factory, email="jefa@labor.cl", name="Marta", principal=True)

    quote_service.handle_apro_ia_credit_webhook(_payload("Aprobada"))

    assert avisos_cliente[0]["contact_email"] == "jefa@labor.cl"
    assert avisos_cliente[0]["contact_name"] == "Marta"


def test_credito_sin_ningun_email_no_avisa_ni_falla(
    db_session_factory, shopify, avisos_cliente
):
    _crear_pedido_sin_cotizacion(db_session_factory, shopify_order_id="555")

    result = quote_service.handle_apro_ia_credit_webhook(_payload("Aprobada"))

    assert result["applied"] is True
    assert avisos_cliente == []
