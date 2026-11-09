"""Link de solicitud de crédito desde el formulario de registro.

``RegistrationRequestService.credit_request_url`` lo entrega solo para una
solicitud pendiente, con ``payment_type = CREDIT`` y reciente: el ``request_id``
es la única credencial del storefront. El RUT sale siempre de la solicitud
guardada.
"""
from __future__ import annotations

import datetime as dt
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
from services import registration_request_service as svc_mod  # noqa: E402

_URL = "https://cliente-qa.apro.cl/creditos/solicitud?token=abc"


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


@pytest.fixture()
def apro(monkeypatch):
    """Registra con qué RUT se pidió el link y devuelve una URL fija."""
    llamadas: list[str] = []

    def _fake(rut: str) -> str | None:
        llamadas.append(rut)
        return _URL

    monkeypatch.setattr(svc_mod, "solicitar_url_credito", _fake)
    return llamadas


def _solicitud(
    get_session,
    *,
    status: str = "PENDING",
    payment_type: str = "CREDIT",
    hace: dt.timedelta = dt.timedelta(minutes=5),
) -> str:
    with get_session() as session:
        row = CompanyRegistrationRequest(
            status=status,
            payload={"rut": "76943201-9", "payment_type": payment_type},
            submitted_email="contacto@empresa.cl",
        )
        session.add(row)
        session.flush()
        row.created_at = dt.datetime.now(dt.UTC) - hace
        session.commit()
        return str(row.id)


def test_entrega_el_link_con_el_rut_de_la_solicitud(bd, apro):
    request_id = _solicitud(bd)
    assert svc_mod.RegistrationRequestService().credit_request_url(request_id) == _URL
    assert apro == ["76943201-9"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"payment_type": "DIRECT"},
        {"status": "APPROVED"},
        {"status": "REJECTED"},
        {"hace": dt.timedelta(hours=25)},
    ],
    ids=["contado", "aprobada", "rechazada", "vencida"],
)
def test_no_aplica_responde_como_no_encontrada(bd, apro, kwargs):
    request_id = _solicitud(bd, **kwargs)
    with pytest.raises(LookupError):
        svc_mod.RegistrationRequestService().credit_request_url(request_id)
    assert apro == []


def test_solicitud_inexistente(bd, apro):
    with pytest.raises(LookupError):
        svc_mod.RegistrationRequestService().credit_request_url(str(uuid.uuid4()))


def test_id_invalido_es_error_de_validacion(bd, apro):
    with pytest.raises(ValueError):
        svc_mod.RegistrationRequestService().credit_request_url("no-es-uuid")


def test_apro_sin_link_es_runtime_error(bd, monkeypatch):
    request_id = _solicitud(bd)
    monkeypatch.setattr(svc_mod, "solicitar_url_credito", lambda rut: None)
    with pytest.raises(RuntimeError):
        svc_mod.RegistrationRequestService().credit_request_url(request_id)


# ── Antes del alta: por RUT ────────────────────────────────────────────────────


def test_por_rut_entrega_el_link_con_el_rut_normalizado(apro):
    assert svc_mod.RegistrationRequestService().credit_request_url_for_rut("76.943.201-9") == _URL
    assert apro == ["76943201-9"]


@pytest.mark.parametrize("rut", ["", "76943201-0", "abc"], ids=["vacio", "dv-malo", "basura"])
def test_por_rut_invalido_es_error_de_validacion(apro, rut):
    with pytest.raises(ValueError):
        svc_mod.RegistrationRequestService().credit_request_url_for_rut(rut)
    assert apro == []


def test_por_rut_apro_sin_link_es_runtime_error(monkeypatch):
    monkeypatch.setattr(svc_mod, "solicitar_url_credito", lambda rut: None)
    with pytest.raises(RuntimeError):
        svc_mod.RegistrationRequestService().credit_request_url_for_rut("76943201-9")
