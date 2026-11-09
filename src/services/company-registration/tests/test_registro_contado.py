"""Registro siempre al contado y link de crédito en el correo de aprobación.

El formulario ya no pregunta la forma de pago: ``create_public_request`` guarda
``DIRECT`` aunque el body diga otra cosa. Al aprobar, el correo presenta al
vendedor asignado y, si la empresa quedó sin crédito, el link estable de
``companies`` que pide el link de APRO recién al hacer clic.
"""
from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))
SHARED_ROOT = SERVICE_ROOT / "shared"
if str(SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(SHARED_ROOT))

# Imports arriba a propósito: se resuelven justo después del conftest que aísla
# los paquetes de este servicio.
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database.base import Base  # noqa: E402
import database.models  # noqa: E402,F401  registra los modelos en Base
from database.models.registration_request import CompanyRegistrationRequest  # noqa: E402
from database.models.user import User  # noqa: E402
from apro import credit_link  # noqa: E402
from notifications import ses_email  # noqa: E402
from services import registration_request_service as svc_mod  # noqa: E402

_URL = "https://apro-ia.apro.cl/insurances/client/abc"


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    """Los modelos usan JSONB (PostgreSQL); en SQLite se compila como JSON."""
    return "JSON"


@pytest.fixture()
def bd(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    @contextmanager
    def _get_session():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(svc_mod, "get_session", _get_session)
    return _get_session


def _body(**extra) -> dict:
    return {
        "company_name": "CO STUDIO SPA",
        "rut": "76943201-9",
        "contact_name": "Ana Pérez",
        "contact_email": "ana@costudio.cl",
        **extra,
    }


@pytest.mark.parametrize("payment_type", [None, "CREDIT", "DIRECT", "otro"])
def test_alta_publica_siempre_queda_al_contado(bd, monkeypatch, payment_type):
    monkeypatch.setattr(svc_mod, "encolar_nueva_solicitud", lambda **kw: None)
    monkeypatch.setattr(svc_mod.RegistrationRequestService, "_get_admin_emails", lambda self: [])
    body = _body() if payment_type is None else _body(payment_type=payment_type)

    svc_mod.RegistrationRequestService().create_public_request(body)

    with bd() as session:
        row = session.query(CompanyRegistrationRequest).one()
        assert row.payload["payment_type"] == "DIRECT"


def test_nombre_del_vendedor_normaliza_espacios(bd):
    user_id = uuid.uuid4()
    with bd() as session:
        session.add(
            User(
                id=user_id,
                cognito_sub=str(uuid.uuid4()),
                email="bryan.ramirez@apro.cl",
                given_name="Bryan ",
                family_name="Ramírez ",
                role="SALES",
                status="ACTIVE",
                codigo_sap="541",
            )
        )
        session.commit()

    assert svc_mod._nombre_vendedor(user_id) == "Bryan Ramírez"


def test_sin_vendedor_no_hay_nombre(bd):
    assert svc_mod._nombre_vendedor(None) is None
    assert svc_mod._nombre_vendedor(uuid.uuid4()) is None


def test_link_del_correo_es_el_estable_de_companies(monkeypatch):
    monkeypatch.setenv("CREDIT_LINK_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("CREDIT_LINK_BASE_URL", "https://companies.example/")
    company_id = str(uuid.uuid4())

    url = credit_link.url_solicitud_credito(company_id)

    assert url.startswith("https://companies.example/api/v1/storefront/company/credit/go?t=")
    assert credit_link.verificar_token(url.split("?t=")[1]) == company_id


def test_sin_configuracion_no_hay_link(monkeypatch):
    monkeypatch.delenv("CREDIT_LINK_SECRET", raising=False)
    monkeypatch.setenv("CREDIT_LINK_BASE_URL", "https://companies.example")
    assert credit_link.url_solicitud_credito(str(uuid.uuid4())) is None


def _correo_aprobacion(monkeypatch, **kw) -> str:
    enviados: list[str] = []
    monkeypatch.setattr(ses_email, "_enviar", lambda to, subject, html: enviados.append(html))
    ses_email.notify_approval(
        contact_email="ana@costudio.cl",
        contact_name="Ana",
        company_name="CO STUDIO SPA",
        **kw,
    )
    return enviados[0]


def test_correo_de_aprobacion_trae_vendedor_y_link(monkeypatch):
    html = _correo_aprobacion(monkeypatch, seller_name="Bryan Ramírez", credit_url=_URL)
    assert "Bryan Ramírez" in html
    assert f'href="{_URL}"' in html
    assert "Solicitar crédito" in html


def test_correo_de_aprobacion_sin_datos_no_muestra_los_bloques(monkeypatch):
    html = _correo_aprobacion(monkeypatch)
    assert "vendedor asignado" not in html
    assert "Solicitar crédito" not in html
