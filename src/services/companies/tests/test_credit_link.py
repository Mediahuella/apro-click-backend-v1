"""Link estable de crédito del correo de aprobación.

APRO invalida el link anterior cada vez que entrega uno nuevo, así que el
correo lleva ``/api/v1/storefront/company/credit/go?t=<token>``: el token
(``apro.credit_link``) identifica a la empresa y el endpoint pide a APRO un
link recién generado al hacer clic.
"""
from __future__ import annotations

import sys
import uuid
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
from apro import credit_link  # noqa: E402
from handlers import companies as handler  # noqa: E402

_APRO = "https://apro-ia.apro.cl/insurances/client/nuevo"


@pytest.fixture(autouse=True)
def secreto(monkeypatch):
    monkeypatch.setenv("CREDIT_LINK_SECRET", "secreto-de-prueba")
    monkeypatch.setenv("CREDIT_LINK_BASE_URL", "https://companies.example")


# ── Token ──────────────────────────────────────────────────────────────────────


def test_token_valido_devuelve_la_empresa():
    company_id = str(uuid.uuid4())
    assert credit_link.verificar_token(credit_link.firmar_token(company_id)) == company_id


def test_token_vencido_no_sirve():
    token = credit_link.firmar_token(str(uuid.uuid4()), ahora=0)
    assert credit_link.verificar_token(token) is None


def test_token_adulterado_no_sirve():
    otra = str(uuid.uuid4())
    payload, firma = credit_link.firmar_token(str(uuid.uuid4())).split(".")
    falso = credit_link._b64(f"{otra}.9999999999".encode())
    assert credit_link.verificar_token(f"{falso}.{firma}") is None
    assert credit_link.verificar_token(f"{payload}.x{firma[1:]}") is None
    assert credit_link.verificar_token("basura") is None


def test_token_de_otro_secreto_no_sirve(monkeypatch):
    token = credit_link.firmar_token(str(uuid.uuid4()))
    monkeypatch.setenv("CREDIT_LINK_SECRET", "otro-secreto")
    assert credit_link.verificar_token(token) is None


# ── Endpoint ───────────────────────────────────────────────────────────────────


def _get(token: str) -> dict:
    evento = {
        "version": "2.0",
        "routeKey": "GET /api/v1/storefront/company/credit/go",
        "rawPath": "/api/v1/storefront/company/credit/go",
        "rawQueryString": f"t={token}",
        "queryStringParameters": {"t": token},
        "headers": {},
        "requestContext": {
            "http": {"method": "GET", "path": "/api/v1/storefront/company/credit/go"},
            "stage": "$default",
        },
        "isBase64Encoded": False,
    }
    return handler.app.resolve(evento, None)


@pytest.fixture()
def empresa(monkeypatch):
    company_id = str(uuid.uuid4())
    datos = {
        "id": company_id,
        "billing_rut": "76943201-9",
        "assigned_sales_user": {"given_name": "Bryan", "family_name": "Ramírez", "codigo_sap": "541"},
    }
    monkeypatch.setattr(
        handler.company_service, "get_company", lambda cid: datos if cid == company_id else None
    )
    return company_id


def test_redirige_al_link_recien_pedido_a_apro(monkeypatch, empresa):
    pedidos: list = []

    def _fake(rut, *, id_sap=None):
        pedidos.append((rut, id_sap))
        return _APRO

    monkeypatch.setattr(handler, "solicitar_url_credito", _fake)

    r = _get(credit_link.firmar_token(empresa))

    assert r["statusCode"] == 302
    assert r["headers"]["Location"] == _APRO
    assert pedidos == [("76943201-9", "541")]


def test_token_invalido_muestra_pagina_sin_llamar_a_apro(monkeypatch, empresa):
    monkeypatch.setattr(handler, "solicitar_url_credito", lambda *a, **k: pytest.fail("no debe llamar"))
    r = _get("basura")
    assert r["statusCode"] == 404
    assert "text/html" in r["headers"]["Content-Type"]
    assert "ya no es válido" in r["body"]


def test_apro_sin_link_muestra_pagina_de_error(monkeypatch, empresa):
    monkeypatch.setattr(handler, "solicitar_url_credito", lambda *a, **k: None)
    r = _get(credit_link.firmar_token(empresa))
    assert r["statusCode"] == 502
    assert "No pudimos abrir" in r["body"]
