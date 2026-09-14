"""Tests del ambiente QA de APRO IA (espejo de salida + rutas ``/qa``).

Cubren la regla central del feature: **solo las empresas de
``APRO_IA_QA_ALLOWED_RUTS`` participan de QA**. Todo lo demás que entre por las
rutas ``/qa`` se descarta sin tocar nada, porque un evento de prueba no puede
crear Draft Orders ni marcar pedidos pagados en la tienda de producción.

También cubren el aislamiento de identificadores: QA y producción matchean
contra columnas distintas (``apro_ia_qa_id`` vs ``apro_ia_id``). Sin eso, un
``id`` de QA puede coincidir con el de una cotización real de otro cliente —
el mismo tipo de bug que ya ocurrió con los folios repetidos (ver
``test_apro_ia_webhook.test_folio_duplicado_no_actualiza_cotizacion_de_otro_cliente``).

No hacen ninguna llamada real a APRO IA, Shopify ni SQS.

Ejecutar con:
  cd backend-v1 && python3 -m pytest src/services/quotes/tests/test_apro_ia_qa.py -v
"""
from __future__ import annotations

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
import database.models  # noqa: E402,F401
from database.models.company import Company  # noqa: E402
from database.models.quote_request import QuoteRequest, QuoteRequestLine  # noqa: E402

from services import apro_ia_client  # noqa: E402
from services import quote_service  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


#: RUT de la empresa de pruebas (Seba SPA) habilitada para QA.
RUT_QA = "76486379-8"
#: Cliente real cualquiera: no debe poder entrar por las rutas /qa.
RUT_AJENO = "96959470-6"


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
def qa_env(monkeypatch):
    """Ambiente QA configurado y con Seba SPA habilitada."""
    monkeypatch.setenv("APRO_IA_QA_ALLOWED_RUTS", RUT_QA)
    monkeypatch.setenv("APRO_IA_BASE_URL_QA", "https://dms-qa.apro.cl")
    monkeypatch.setenv("APRO_IA_BEARER_TOKEN_QA", "token-qa")
    monkeypatch.setenv("APRO_IA_BASE_URL", "https://dms.apro.cl")
    monkeypatch.setenv("APRO_IA_BEARER_TOKEN", "token-prod")


def _quote(session, *, rut, apro_ia_id=None, apro_ia_qa_id=None, folio="CO-000100"):
    company = Company(
        id=uuid_mod.uuid4(),
        name="Empresa",
        billing_rut=rut,
        billing_razon_social="Empresa",
        shopify_company_id="gid://shopify/Company/1",
    )
    session.add(company)
    quote = QuoteRequest(
        id=uuid_mod.uuid4(),
        company_id=company.id,
        shopify_company_id="gid://shopify/Company/1",
        shop_domain="aproclick.myshopify.com",
        folio=folio,
        apro_ia_id=apro_ia_id,
        apro_ia_folio=folio if apro_ia_id else None,
        apro_ia_qa_id=apro_ia_qa_id,
        apro_ia_qa_folio=folio if apro_ia_qa_id else None,
        status="COTIZADA",
    )
    quote.lines.append(
        QuoteRequestLine(
            id=uuid_mod.uuid4(),
            line_number=1,
            variant_gid="gid://shopify/ProductVariant/1",
            sku="SKU-1",
            title="Producto",
            quantity=1,
            negotiated_unit_price=Decimal("1000"),
        )
    )
    session.add(quote)
    session.flush()
    return quote


def _payload(event="quotation.approved", status="Autorizada", *, rut, ident, folio):
    return {
        "event": event,
        "folio": folio,
        "id": ident,
        "status": status,
        "client": {"name": "Empresa", "rut": rut},
        "products": [
            {"sku": "SKU-1", "name": "Producto", "amount": 1, "price": 1000, "total": 1000}
        ],
    }


# ---------------------------------------------------------------------------
# Allowlist — la guarda que evita que QA toque clientes reales
# ---------------------------------------------------------------------------
def test_allowlist_normaliza_el_rut():
    """El RUT llega sin separadores desde el DMS y con guión desde nuestra BD."""
    assert quote_service._qa_habilita_rut("764863798")
    assert quote_service._qa_habilita_rut("76486379-8")
    assert quote_service._qa_habilita_rut("76.486.379-8")
    assert not quote_service._qa_habilita_rut(RUT_AJENO)
    assert not quote_service._qa_habilita_rut("")
    assert not quote_service._qa_habilita_rut(None)


def test_allowlist_vacia_apaga_qa(monkeypatch):
    monkeypatch.setenv("APRO_IA_QA_ALLOWED_RUTS", "")
    assert not quote_service._qa_habilita_rut(RUT_QA)


def test_evento_qa_de_otro_cliente_se_descarta(db_session_factory):
    """Un evento de QA para un cliente real no toca su cotización."""
    with db_session_factory() as session:
        quote = _quote(session, rut=RUT_AJENO, apro_ia_id="prod-1")
        quote_id = quote.id
        session.commit()

    res = quote_service.handle_apro_ia_webhook(
        _payload(rut=RUT_AJENO, ident="prod-1", folio="CO-000100"),
        env=apro_ia_client.ENV_QA,
    )

    assert res == {"received": True, "ignored": True, "matched": False}
    with db_session_factory() as session:
        assert session.get(QuoteRequest, quote_id).apro_ia_status is None


def test_transferencia_qa_de_otro_cliente_se_descarta(db_session_factory, monkeypatch):
    """La guarda corre antes de resolver el pedido: no se toca Shopify."""
    def _explota(*a, **kw):  # pragma: no cover - no debe ejecutarse
        raise AssertionError("no debió buscarse el pedido")

    monkeypatch.setattr(quote_service, "_find_transfer_target", _explota)

    res = quote_service.handle_apro_ia_transfer_webhook(
        {"folio": "TRA1", "rut": RUT_AJENO, "status": "Aprobada", "order": "1084"},
        env=apro_ia_client.ENV_QA,
    )

    assert res == {"received": True, "ignored": True, "matched": False}


def test_transferencia_prod_no_pasa_por_la_allowlist(db_session_factory, monkeypatch):
    """La ruta productiva sigue atendiendo a cualquier cliente, como hoy."""
    llamadas = []
    monkeypatch.setattr(
        quote_service,
        "_find_transfer_target",
        lambda *a, **kw: llamadas.append(a) or None,
    )

    res = quote_service.handle_apro_ia_transfer_webhook(
        {"folio": "TRA1", "rut": RUT_AJENO, "status": "Aprobada", "order": "1084"}
    )

    assert llamadas, "la ruta productiva debe seguir resolviendo el pedido"
    assert res["matched"] is False


# ---------------------------------------------------------------------------
# Aislamiento de identificadores entre ambientes
# ---------------------------------------------------------------------------
def test_evento_qa_no_matchea_por_columnas_de_produccion(db_session_factory):
    """Un id de QA que colisiona con un apro_ia_id real no pisa esa cotización.

    Caso a evitar: QA emite ``id=42`` y existe una cotización productiva de la
    misma empresa con ``apro_ia_id=42``.
    """
    with db_session_factory() as session:
        quote = _quote(session, rut=RUT_QA, apro_ia_id="42")
        quote_id = quote.id
        session.commit()

    res = quote_service.handle_apro_ia_webhook(
        _payload(rut=RUT_QA, ident="42", folio="CO-000100"),
        env=apro_ia_client.ENV_QA,
    )

    assert res == {"received": True, "matched": False, "created": False}
    with db_session_factory() as session:
        assert session.get(QuoteRequest, quote_id).apro_ia_status is None


def test_evento_prod_no_matchea_por_columnas_de_qa(db_session_factory):
    """Simétrico: producción ignora las columnas del espejo."""
    with db_session_factory() as session:
        quote = _quote(session, rut=RUT_QA, apro_ia_qa_id="42")
        quote_id = quote.id
        session.commit()

    res = quote_service.handle_apro_ia_webhook(
        _payload(rut=RUT_QA, ident="42", folio="CO-000100")
    )

    assert res.get("matched") is False
    with db_session_factory() as session:
        assert session.get(QuoteRequest, quote_id).apro_ia_status is None


def test_evento_qa_matchea_su_espejo(db_session_factory, monkeypatch):
    with db_session_factory() as session:
        quote = _quote(session, rut=RUT_QA, apro_ia_id="prod-1", apro_ia_qa_id="qa-1")
        quote_id = quote.id
        session.commit()

    monkeypatch.setattr(
        quote_service, "encolar_cotizacion_apro_ia_estado", lambda **kw: None
    )

    res = quote_service.handle_apro_ia_webhook(
        _payload(rut=RUT_QA, ident="qa-1", folio="CO-000100"),
        env=apro_ia_client.ENV_QA,
    )

    assert res["matched"] is True
    with db_session_factory() as session:
        assert session.get(QuoteRequest, quote_id).apro_ia_status == "Autorizada"


def test_quotation_created_en_qa_no_autocrea(db_session_factory, monkeypatch):
    """Auto-crear desde QA abriría un Draft Order real en la tienda productiva."""
    def _explota(*a, **kw):  # pragma: no cover - no debe ejecutarse
        raise AssertionError("no debió intentar crear la cotización")

    monkeypatch.setattr(quote_service, "_create_quote_from_apro_ia_payload", _explota)

    res = quote_service.handle_apro_ia_webhook(
        _payload("quotation.created", "Pendiente", rut=RUT_QA, ident="qa-9", folio="CO-9"),
        env=apro_ia_client.ENV_QA,
    )

    assert res == {"received": True, "matched": False, "created": False}


# ---------------------------------------------------------------------------
# Espejo de salida
# ---------------------------------------------------------------------------
def test_espejo_crea_en_qa_y_guarda_identificadores(db_session_factory, monkeypatch):
    llamadas = []

    def _crear(**kw):
        llamadas.append(kw)
        return {"id": "qa-77", "folio": "QA-000077"}

    monkeypatch.setattr(apro_ia_client, "crear_cotizacion", _crear)
    monkeypatch.setattr(quote_service, "_seller_sap_code", lambda *a, **kw: "123")

    with db_session_factory() as session:
        quote = _quote(session, rut=RUT_QA, apro_ia_id="prod-1")
        quote_service._espejar_en_qa(
            session,
            quote,
            client_rut=RUT_QA,
            client_nombre="Seba SPA",
            seller_email="v@apro.cl",
            productos=[{"sku": "SKU-1"}],
        )
        assert quote.apro_ia_qa_id == "qa-77"
        assert quote.apro_ia_qa_folio == "QA-000077"
        # Producción intacta: el espejo no puede pisar el folio oficial.
        assert quote.apro_ia_id == "prod-1"
        assert quote.folio == "CO-000100"

    assert llamadas[0]["env"] == apro_ia_client.ENV_QA


def test_espejo_no_corre_para_empresas_fuera_de_la_allowlist(
    db_session_factory, monkeypatch
):
    def _explota(**kw):  # pragma: no cover - no debe ejecutarse
        raise AssertionError("no debió llamar a APRO IA QA")

    monkeypatch.setattr(apro_ia_client, "crear_cotizacion", _explota)

    with db_session_factory() as session:
        quote = _quote(session, rut=RUT_AJENO, apro_ia_id="prod-1")
        quote_service._espejar_en_qa(
            session,
            quote,
            client_rut=RUT_AJENO,
            client_nombre="Otra",
            seller_email="v@apro.cl",
            productos=[{"sku": "SKU-1"}],
        )
        assert quote.apro_ia_qa_id is None


def test_espejo_se_saltea_sin_credenciales(db_session_factory, monkeypatch):
    monkeypatch.setenv("APRO_IA_BEARER_TOKEN_QA", "")

    def _explota(**kw):  # pragma: no cover - no debe ejecutarse
        raise AssertionError("no debió llamar a APRO IA QA")

    monkeypatch.setattr(apro_ia_client, "crear_cotizacion", _explota)

    with db_session_factory() as session:
        quote = _quote(session, rut=RUT_QA, apro_ia_id="prod-1")
        quote_service._espejar_en_qa(
            session,
            quote,
            client_rut=RUT_QA,
            client_nombre="Seba SPA",
            seller_email="v@apro.cl",
            productos=[{"sku": "SKU-1"}],
        )
        assert quote.apro_ia_qa_id is None


def test_espejo_traga_el_error_de_qa(db_session_factory, monkeypatch):
    """Si QA falla, el flujo productivo no se entera."""
    def _falla(**kw):
        raise RuntimeError("QA caído")

    monkeypatch.setattr(apro_ia_client, "crear_cotizacion", _falla)
    monkeypatch.setattr(quote_service, "_seller_sap_code", lambda *a, **kw: "123")

    with db_session_factory() as session:
        quote = _quote(session, rut=RUT_QA, apro_ia_id="prod-1")
        quote_service._espejar_en_qa(  # no debe propagar
            session,
            quote,
            client_rut=RUT_QA,
            client_nombre="Seba SPA",
            seller_email="v@apro.cl",
            productos=[{"sku": "SKU-1"}],
        )
        assert quote.apro_ia_qa_id is None


# ---------------------------------------------------------------------------
# Cliente HTTP — resolución de ambiente
# ---------------------------------------------------------------------------
def test_cliente_resuelve_url_y_token_por_ambiente():
    assert apro_ia_client._base_url() == "https://dms.apro.cl"
    assert apro_ia_client._base_url(apro_ia_client.ENV_QA) == "https://dms-qa.apro.cl"
    assert apro_ia_client._token() == "token-prod"
    assert apro_ia_client._token(apro_ia_client.ENV_QA) == "token-qa"


def test_cliente_rechaza_ambiente_desconocido():
    with pytest.raises(ValueError):
        apro_ia_client._base_url("staging")


def test_env_configurado(monkeypatch):
    assert apro_ia_client.env_configurado(apro_ia_client.ENV_QA)
    monkeypatch.setenv("APRO_IA_BASE_URL_QA", "")
    assert not apro_ia_client.env_configurado(apro_ia_client.ENV_QA)
