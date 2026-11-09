"""Tests del cambio de lista de precios (``PUT /api/v1/companies/{id}/price-list``).

Los vendedores (SALES) pueden cambiar la lista de precios solo de las empresas
de su cartera —vinculadas en ``user_companies`` o registradas desde su link—;
SUPERADMIN/ADMIN, de cualquiera. Cada cambio queda en ``audit_logs``.

Ejecutar con:
  cd backend-v1 && python3 -m pytest src/services/companies/tests/test_price_list.py -v
"""
from __future__ import annotations

import sys
import uuid as uuid_mod
from contextlib import contextmanager
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))
SHARED_ROOT = SERVICE_ROOT / "shared"
if str(SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(SHARED_ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from database.base import Base  # noqa: E402
import database.models  # noqa: E402,F401  registra los modelos en Base
from database.models.audit_log import AuditLog  # noqa: E402
from database.models.company import Company  # noqa: E402
from database.models.user import User  # noqa: E402
from database.models.user_company import UserCompany  # noqa: E402
from services import company_service as company_service_mod  # noqa: E402


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture()
def escenario(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    @contextmanager
    def _get_session():
        session = Session(engine)
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(company_service_mod, "get_session", _get_session)
    movidas: list[tuple] = []

    def _sync(shopify_company_id, old, new):
        movidas.append((shopify_company_id, old, new))
        return "ok"

    monkeypatch.setattr(company_service_mod, "sync_company_catalog", _sync)

    with _get_session() as s:
        vendedor = User(id=uuid_mod.uuid4(), cognito_sub="v", email="v@apro.cl", role="SALES", status="ACTIVE")
        admin = User(id=uuid_mod.uuid4(), cognito_sub="a", email="a@apro.cl", role="ADMIN", status="ACTIVE")
        kpi = User(id=uuid_mod.uuid4(), cognito_sub="k", email="k@apro.cl", role="KPI_VISUALIZERS", status="ACTIVE")
        s.add_all([vendedor, admin, kpi])
        s.flush()
        asignada = Company(id=uuid_mod.uuid4(), name="Asignada", company_type="SMALL", shopify_company_id="1")
        referida = Company(
            id=uuid_mod.uuid4(), name="Referida", company_type="SMALL", referred_by_user_id=vendedor.id
        )
        ajena = Company(id=uuid_mod.uuid4(), name="Ajena", company_type="SMALL")
        s.add_all([asignada, referida, ajena])
        s.flush()
        s.add(UserCompany(user_id=vendedor.id, company_id=asignada.id))
        s.commit()
        datos = {
            "vendedor": {"id": str(vendedor.id), "role": "SALES"},
            "admin": {"id": str(admin.id), "role": "ADMIN"},
            "kpi": {"id": str(kpi.id), "role": "KPI_VISUALIZERS"},
            "asignada": str(asignada.id),
            "referida": str(referida.id),
            "ajena": str(ajena.id),
        }
    return datos, _get_session, movidas


SERVICE = company_service_mod.CompanyService


def test_vendedor_cambia_empresa_de_su_cartera(escenario):
    datos, get_session, movidas = escenario
    res = SERVICE().change_price_list(datos["asignada"], "MEDIUM", actor=datos["vendedor"])
    assert res["company_type"] == "MEDIUM"
    assert res["catalog_sync"] == "ok"
    assert movidas == [("1", "SMALL", "MEDIUM")]
    with get_session() as s:
        log = s.scalars(select(AuditLog)).one()
        assert log.entity_id == datos["asignada"]
        assert log.action == "price_list_changed"
        assert str(log.actor_user_id) == datos["vendedor"]["id"]
        assert log.metadata_ == {"from": "SMALL", "to": "MEDIUM", "role": "SALES"}


def test_vendedor_cambia_empresa_referida_por_su_link(escenario):
    datos, _, _ = escenario
    res = SERVICE().change_price_list(datos["referida"], "BIG", actor=datos["vendedor"])
    assert res["company_type"] == "BIG"


def test_vendedor_no_puede_cambiar_empresa_ajena(escenario):
    datos, get_session, movidas = escenario
    with pytest.raises(LookupError):
        SERVICE().change_price_list(datos["ajena"], "BIG", actor=datos["vendedor"])
    assert movidas == []
    with get_session() as s:
        assert s.get(Company, uuid_mod.UUID(datos["ajena"])).company_type == "SMALL"
        assert s.scalars(select(AuditLog)).all() == []


def test_admin_cambia_cualquier_empresa(escenario):
    datos, _, _ = escenario
    res = SERVICE().change_price_list(datos["ajena"], "BIG", actor=datos["admin"])
    assert res["company_type"] == "BIG"


def test_otro_rol_no_puede(escenario):
    datos, _, _ = escenario
    with pytest.raises(PermissionError):
        SERVICE().change_price_list(datos["asignada"], "BIG", actor=datos["kpi"])


def test_lista_invalida(escenario):
    datos, _, _ = escenario
    with pytest.raises(ValueError):
        SERVICE().change_price_list(datos["asignada"], "VIP", actor=datos["admin"])


def test_misma_lista_no_toca_shopify_ni_audita(escenario):
    datos, get_session, movidas = escenario
    res = SERVICE().change_price_list(datos["asignada"], "SMALL", actor=datos["vendedor"])
    assert res["catalog_sync"] == "skipped"
    assert movidas == []
    with get_session() as s:
        assert s.scalars(select(AuditLog)).all() == []


# ---------------------------------------------------------------------------
# Rutas de administración: solo SUPERADMIN/ADMIN
# ---------------------------------------------------------------------------
from handlers import companies as companies_handler  # noqa: E402


class _Ctx:
    function_name = "companies"
    memory_limit_in_mb = 512
    invoked_function_arn = "arn:aws:lambda:us-east-2:000000000000:function:companies"
    aws_request_id = "00000000-0000-0000-0000-000000000000"


def _llamar(method: str, path: str, body: str = "{}") -> dict:
    event = {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json", "authorization": "Bearer x"},
        "requestContext": {
            "http": {"method": method, "path": path, "protocol": "HTTP/1.1"},
            "domainName": "example.com",
            "stage": "$default",
            "requestId": "test",
        },
        "body": body,
        "isBase64Encoded": False,
    }
    return companies_handler.lambda_handler(event, _Ctx())


RUTAS_ADMIN = [
    ("PUT", "", '{"company_type": "BIG"}'),
    ("PUT", "/assigned-user", '{"user_id": null}'),
    ("DELETE", "", ""),
]


@pytest.mark.parametrize("method,suffix,body", RUTAS_ADMIN)
def test_vendedor_no_usa_rutas_de_administracion(monkeypatch, method, suffix, body):
    llamadas: list = []
    monkeypatch.setattr(
        companies_handler, "get_user_by_cognito_access_token", lambda t: {"id": "u", "role": "SALES"}
    )
    for nombre in ("update_company", "set_assigned_user", "delete_company"):
        monkeypatch.setattr(
            companies_handler.company_service, nombre, lambda *a, **k: llamadas.append(1) or {}
        )
    cid = str(uuid_mod.uuid4())
    resp = _llamar(method, f"/api/v1/companies/{cid}{suffix}", body)
    assert resp["statusCode"] == 403
    assert llamadas == []


@pytest.mark.parametrize("method,suffix,body", RUTAS_ADMIN)
def test_admin_usa_rutas_de_administracion(monkeypatch, method, suffix, body):
    monkeypatch.setattr(
        companies_handler, "get_user_by_cognito_access_token", lambda t: {"id": "u", "role": "ADMIN"}
    )
    for nombre in ("update_company", "set_assigned_user", "delete_company"):
        monkeypatch.setattr(companies_handler.company_service, nombre, lambda *a, **k: {})
    cid = str(uuid_mod.uuid4())
    resp = _llamar(method, f"/api/v1/companies/{cid}{suffix}", body)
    assert resp["statusCode"] == 200
